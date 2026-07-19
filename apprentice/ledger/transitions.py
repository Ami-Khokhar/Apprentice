from __future__ import annotations

import time
from collections.abc import Mapping

from apprentice.config import Policy
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.models import Bucket, FailureCause, Run, RunState

LEGAL_RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.REHEARSING}),
    RunState.REHEARSING: frozenset({RunState.REHEARSED, RunState.REHEARSAL_FAILED}),
    RunState.REHEARSAL_FAILED: frozenset(),
    RunState.REHEARSED: frozenset(
        {
            RunState.APPROVAL_PENDING,
            RunState.VETO_PENDING,
            RunState.AUTHORIZED,
            RunState.DENIED,
            # A stored rehearsal that fails the pre-activation re-check (its
            # recorded actions no longer hash to the stored action_plan_hash,
            # or a stored check flipped to failed) must fail closed rather
            # than ever reach an approval boundary.
            RunState.REHEARSAL_FAILED,
        }
    ),
    RunState.APPROVAL_PENDING: frozenset({RunState.AUTHORIZED, RunState.DENIED}),
    RunState.VETO_PENDING: frozenset({RunState.AUTHORIZED, RunState.VETOED}),
    RunState.AUTHORIZED: frozenset({RunState.EXECUTING, RunState.EXPIRED}),
    RunState.DENIED: frozenset(),
    RunState.EXECUTING: frozenset({RunState.SUCCEEDED, RunState.FAILED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.VETOED: frozenset(),
    RunState.EXPIRED: frozenset(),
}


class InvalidTransitionError(ConflictError):
    def __init__(self, current: RunState, requested: RunState) -> None:
        super().__init__(f"Invalid run transition: {current.value} -> {requested.value}")
        self.current = current
        self.requested = requested


def validate_transition(
    current: RunState | str, requested: RunState | str
) -> tuple[RunState, RunState]:
    current_state = RunState(current)
    requested_state = RunState(requested)
    if requested_state not in LEGAL_RUN_TRANSITIONS[current_state]:
        raise InvalidTransitionError(current_state, requested_state)
    return current_state, requested_state


def apply_idle_decay(
    repository: Repository, bucket: Bucket, policy: Policy, *, now: float
) -> Bucket:
    """Lazily demote a bucket one level for sustained inactivity.

    Computed purely from ``last_activity_at``/``last_decay_at`` on read ---
    there is no background job. The persisted ``last_decay_at`` bookmark is
    what keeps a capability that stays idle from draining a level on every
    subsequent read: a second call before a fresh ``decay_idle_days`` window
    has elapsed since the last decay is a no-op.
    """
    return repository.apply_idle_decay_if_due(bucket.id, policy, now=now)


def confirm_promotion(
    repository: Repository, bucket_id: int, policy: Policy, *, now: float | None = None
) -> Bucket:
    """Apply an already-computed promotion only after explicit user confirmation.

    Re-evaluates eligibility against the live ledger at confirmation time ---
    never trusts a caller-supplied target level --- and raises if the
    capability is no longer eligible (e.g. a demotion landed between the
    dashboard render and the user's click).
    """
    resolved_now = time.time() if now is None else now
    return repository.confirm_promotion_if_eligible(bucket_id, policy, now=resolved_now)


def record_verified_outcome(
    service: object,
    run_id: str,
    *,
    succeeded: bool,
    failure_cause: FailureCause | str | None = None,
) -> tuple[Run, object]:
    """Compatibility entry point that delegates authority to RunService."""
    return service.record_verified_outcome(  # type: ignore[attr-defined,no-any-return]
        run_id,
        succeeded=succeeded,
        failure_cause=failure_cause,
    )
