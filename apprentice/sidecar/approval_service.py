"""The durable L1-L4 approval/veto boundary (plan §6.4).

``ApprovalService`` is the single place a rehearsed plan's activation is
decided. It never receives production actions from a caller --- it re-reads
everything (run, stored rehearsal checks, stored action plan, bucket trust
level) from the sidecar's own ledger by ``run_id`` alone, so a model that
calls ``activate_rehearsed_plan(run_id)`` can never supply or alter what
actually gets replayed.

Before honoring any approval interruption, the six stored rehearsal checks
and the stored action plan's hash are re-verified against the freshly loaded
``run_actions`` rows: a run whose stored evidence no longer supports its own
``rehearsed`` state fails closed into ``rehearsal_failed`` rather than ever
reaching an approval boundary.

L1 has no autonomy and is denied outright. L2 pauses in ``approval_pending``
until a human resolves it. L3 opens exactly one veto countdown (persisted in
SQLite, never one per browser step) and auto-authorizes when the deadline
passes unless it was canceled first; a critical-risk bucket is capped at L3
by the bucket model itself, so it can never reach L4's direct auto-authorize.
L4 authorizes immediately. Veto resolution is a transactional
compare-and-swap on the ``vetoes`` row: a canceled or expired veto can never
be resolved twice, and pending vetoes survive a fresh process opening the
same database file. Both ends of a veto's life are single-transaction
compounds (``Repository.begin_veto_window`` and
``Repository.resolve_veto_with_event``): the veto row is created atomically
with the run's ``veto_pending`` transition, and resolved atomically with the
run's terminal transition/event, so a crash between them can never strand a
run with no veto row, or a resolved veto whose run never actually moved.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from apprentice.canonical import digest
from apprentice.config import Policy
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.models import Run, VetoStatus
from apprentice.sidecar.run_service import RunService
from apprentice.twin.invariants import CHECK_NAMES

__all__ = [
    "ApprovalOutcome",
    "ApprovalService",
    "ChecksFailedError",
    "NotRehearsedError",
    "VetoAlreadyResolvedError",
]

ApprovalKind = Literal["denied", "approval_pending", "veto_pending", "authorized"]

# The bucket trust levels this task's ladder recognizes for activation.
_LEVEL_NO_AUTONOMY = 1
_LEVEL_APPROVAL = 2
_LEVEL_VETO = 3
_LEVEL_AUTONOMOUS = 4


class NotRehearsedError(ConflictError):
    """A caller tried to activate a run that is not (yet, or no longer) rehearsed."""


class ChecksFailedError(ConflictError):
    """The stored rehearsal evidence no longer supports the run's ``rehearsed`` state."""


class VetoAlreadyResolvedError(ConflictError):
    """A veto that was already canceled or had already expired cannot resolve again."""


@dataclass(frozen=True)
class ApprovalOutcome:
    """What handling one activation interruption produced."""

    kind: ApprovalKind
    run: Run
    veto_deadline: float | None = None


class ApprovalService:
    def __init__(
        self,
        run_service: RunService,
        *,
        clock: Callable[[], float] = time.time,
        veto_seconds: float,
        policy: Policy | None = None,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._run_service = run_service
        self._repository: Repository = run_service.repository
        self._clock = clock
        self._veto_seconds = veto_seconds
        self._policy = policy
        self._id_factory = id_factory

    def begin_activation(
        self, run_id: str, *, sdk_state_text: str | None = None
    ) -> ApprovalOutcome:
        """Handle one ``activate_rehearsed_plan(run_id)`` interruption.

        Re-reads everything from the ledger by ``run_id`` alone: the caller
        can never supply or alter which actions are eligible to replay.

        ``sdk_state_text`` is the SDK's own serialized, resumable
        ``RunState`` (produced by the isolated ``SDKStateStore`` adapter ---
        this service never imports or inspects the Agents SDK itself, it
        only stores and returns this text verbatim). Only L2/L3 persist it:
        L1/L4 resolve within this same call, so there is no real time gap
        to survive.
        """
        run = self._repository.get_run(run_id)
        if run.state.value != "rehearsed":
            raise NotRehearsedError(
                f"Run {run_id} is {run.state.value}, not eligible for activation"
            )
        self._verify_stored_rehearsal(run)

        bucket = self._repository.get_bucket_by_id(run.bucket_id)
        if self._policy is not None:
            bucket = self._repository.apply_idle_decay_if_due(
                bucket.id, self._policy, now=self._clock()
            )
        effective_level = self._effective_level(bucket)
        if effective_level <= _LEVEL_NO_AUTONOMY:
            authorized_run, _verdict, _event = self._run_service.record_system_denial(run_id)
            return ApprovalOutcome(kind="denied", run=authorized_run)
        if effective_level == _LEVEL_APPROVAL:
            pending_run = self._run_service.begin_approval_pending(
                run_id, sdk_state_text=sdk_state_text, now=self._clock()
            )
            return ApprovalOutcome(kind="approval_pending", run=pending_run)
        if effective_level == _LEVEL_VETO:
            now = self._clock()
            deadline = now + self._veto_seconds
            pending_run, _veto = self._run_service.begin_veto_window(
                run_id,
                veto_id=self._id_factory(),
                deadline=deadline,
                sdk_state_text=sdk_state_text,
                now=now,
            )
            return ApprovalOutcome(kind="veto_pending", run=pending_run, veto_deadline=deadline)

        assert effective_level >= _LEVEL_AUTONOMOUS
        authorized_run, _verdict, _event = self._run_service.authorize_from_rehearsed(run_id)
        return ApprovalOutcome(kind="authorized", run=authorized_run)

    def _effective_level(self, bucket) -> int:
        levels = [bucket.level]
        buckets_by_name = {item.name: item for item in self._repository.list_buckets()}
        for name in bucket.tool_versions:
            dependency = buckets_by_name.get(name)
            if dependency is None or dependency.kind.value != "tool":
                continue
            if self._policy is not None:
                dependency = self._repository.apply_idle_decay_if_due(
                    dependency.id, self._policy, now=self._clock()
                )
            levels.append(dependency.level)
        if self._policy is not None:
            levels.append(self._policy.risk[bucket.risk_class.value].max_level)
        return min(levels)

    def resolve_approval(
        self, run_id: str, *, approved: bool, reason: str | None = None
    ) -> Run:
        """Resolve an L2 ``approval_pending`` run: exactly one human decision."""
        if approved:
            run, _verdict, _event = self._run_service.record_approval(run_id)
        else:
            detail = {"reason": reason} if reason else None
            run, _verdict, _event = self._run_service.record_denial(run_id, detail=detail)
        return run

    def pop_sdk_state(self, run_id: str) -> str | None:
        """Read and clear a run's persisted SDK resume state; consumed exactly once.

        A caller resuming an SDK run after approval/veto-expiry reads this
        once to deserialize (through ``SDKStateStore``) and continue the
        same run; it is cleared immediately so a second resume attempt finds
        nothing left to resume from.
        """
        return self._repository.consume_sdk_state(run_id)

    def cancel_veto(self, run_id: str) -> Run:
        """Cancel an L3 veto before its deadline; raises if already resolved."""
        resolved = self._run_service.cancel_veto(run_id, now=self._clock())
        if resolved is None:
            raise VetoAlreadyResolvedError(f"Veto for run {run_id} was already resolved")
        run, _event = resolved
        return run

    def resolve_veto_expiry(self, run_id: str) -> Run | None:
        """Authorize an L3 run whose veto deadline has passed; idempotent no-op otherwise.

        Returns ``None`` when the deadline has not yet passed, or the veto
        was already resolved (canceled or previously expired) --- a repeated
        call after the first successful resolution always returns ``None``,
        never resolving (or authorizing) the run a second time.
        """
        veto = self._repository.get_veto(run_id)
        now = self._clock()
        if veto.status is not VetoStatus.PENDING or now < veto.deadline:
            return None
        resolved = self._run_service.authorize_after_veto_expiry(run_id, now=now)
        if resolved is None:
            return None
        run, _verdict, _event = resolved
        return run

    def _verify_stored_rehearsal(self, run: Run) -> None:
        """Re-verify the stored rehearsal's checks and action-plan hash before activating.

        A passing rehearsal can only ever reach ``rehearsed`` after all six
        named checks passed (task 5); this is defense-in-depth against the
        stored evidence itself having been corrupted or tampered with after
        the fact, not a re-run of the checks.
        """
        checks = self._repository.get_rehearsal_checks(run.id)
        actions = self._repository.get_run_actions(run.id)
        recomputed_hash = digest([action.model_dump(mode="json") for action in actions])
        checks_ok = set(checks) == set(CHECK_NAMES) and all(checks.values())
        hash_ok = run.action_plan_hash is not None and recomputed_hash == run.action_plan_hash
        if not (checks_ok and hash_ok):
            self._run_service.reject_activation(run.id)
            raise ChecksFailedError(
                f"Run {run.id}'s stored rehearsal evidence no longer supports activation"
            )
