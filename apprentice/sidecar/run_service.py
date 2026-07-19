from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import TypeAdapter

from apprentice.canonical import InvalidBaseUrlError, digest
from apprentice.config import Policy
from apprentice.ledger.repository import (
    ConflictError,
    EvidenceConflictError,
    Repository,
    UnknownBucketError,
    UnknownRunError,
)
from apprentice.ledger.transitions import InvalidTransitionError, validate_transition
from apprentice.models import (
    ApprovalAttention,
    Bucket,
    DemonstrationRole,
    Event,
    EventType,
    FailureCause,
    JsonValue,
    RecordedAction,
    Run,
    RunState,
    Severity,
    Verdict,
    VerdictActor,
    VerdictDecision,
    Veto,
    VetoStatus,
)
from apprentice.twin.invariants import CHECK_NAMES

_INPUTS_ADAPTER = TypeAdapter(dict[str, JsonValue])
_DETAIL_ADAPTER = TypeAdapter(dict[str, JsonValue])


InvalidTargetError = InvalidBaseUrlError


class UnknownEventTypeError(ValueError):
    """Retained as a typed boundary error; raw event insertion is not public."""


FAILURE_SEVERITY: dict[FailureCause, Severity] = {
    FailureCause.EXECUTION_ERROR: Severity.MINOR,
    FailureCause.PLAN_MISMATCH: Severity.MAJOR,
    FailureCause.UNSAFE_SIDE_EFFECT: Severity.CRITICAL,
}


class RunService:
    def __init__(
        self,
        repository: Repository,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        policy: Policy | None = None,
    ) -> None:
        self.repository = repository
        self._clock = clock
        self._id_factory = id_factory
        self._twin_pass_weight = policy.twin_pass_weight if policy is not None else 0.25
        self._severity_demotion = (
            {
                Severity.MINOR: 0,
                Severity.MAJOR: policy.severity_demotion.major,
                Severity.CRITICAL: policy.severity_demotion.critical,
            }
            if policy is not None
            else {
                Severity.MINOR: 0,
                Severity.MAJOR: 1,
                Severity.CRITICAL: 2,
            }
        )
        self._approval_weights = (
            {
                ApprovalAttention.RUBBER_STAMP: policy.signal_weights.rubber_stamp,
                ApprovalAttention.DEFAULT: policy.signal_weights.default,
                ApprovalAttention.ENGAGED: policy.signal_weights.engaged,
            }
            if policy is not None
            else {
                ApprovalAttention.RUBBER_STAMP: 0.3,
                ApprovalAttention.DEFAULT: 1.0,
                ApprovalAttention.ENGAGED: 1.2,
            }
        )

    def activate_capability(self, bucket_id: int, *, reviewed_playbook: Any) -> Bucket:
        """Activate L1 only after two persisted training demonstrations and review."""
        bucket = self.repository.get_bucket_by_id(bucket_id)
        return self.repository.register_reviewed_playbook(
            bucket_id,
            reviewed_playbook,
            version=bucket.playbook_version,
        )

    def register_self_authored_tool(
        self,
        name: str,
        *,
        artifact_digest: str,
        risk_class: str = "low",
        target_base_url: str = "http://127.0.0.1",
        approved_hosts: list[str] | None = None,
    ) -> Bucket:
        """Activate a reviewed, hash-pinned self-authored tool at L1."""
        return self.repository.register_self_authored_tool(
            name,
            artifact_digest=artifact_digest,
            risk_class=risk_class,
            target_base_url=target_base_url,
            approved_hosts=approved_hosts,
        )

    def declare_tool_dependency(
        self,
        bucket_id: int,
        *,
        tool_name: str,
        artifact_digest: str,
    ) -> Bucket:
        """Bind a playbook to the exact reviewed artifact of one tool."""
        return self.repository.declare_tool_dependency(
            bucket_id,
            tool_name=tool_name,
            artifact_digest=artifact_digest,
        )

    def create_run(
        self,
        bucket_name: str,
        inputs: dict[str, JsonValue],
    ) -> Run:
        bucket = self.repository.get_bucket_by_name(bucket_name)
        validated_inputs = _INPUTS_ADAPTER.validate_python(inputs)
        return self.repository._create_run(
            run_id=self._id_factory(),
            bucket=bucket,
            inputs=validated_inputs,
            input_digest=digest(validated_inputs),
            created_at=self._clock(),
        )

    def _transition(
        self,
        run_id: str,
        expected_state: RunState | str,
        new_state: RunState | str,
        *,
        reason: str,
    ) -> Run:
        expected, requested = validate_transition(expected_state, new_state)
        return self.repository._transition_run(
            run_id,
            expected_state=expected,
            new_state=requested,
            changed_at=self._clock(),
            reason=reason,
        )

    def start_rehearsal(self, run_id: str) -> Run:
        return self._transition(
            run_id,
            RunState.CREATED,
            RunState.REHEARSING,
            reason="rehearsal_started",
        )

    def request_approval(self, run_id: str) -> Run:
        return self._transition(
            run_id,
            RunState.REHEARSED,
            RunState.APPROVAL_PENDING,
            reason="approval_requested",
        )

    def begin_approval_pending(
        self, run_id: str, *, sdk_state_text: str | None, now: float
    ) -> Run:
        validate_transition(RunState.REHEARSED, RunState.APPROVAL_PENDING)
        return self.repository.begin_approval_pending(
            run_id, sdk_state_text=sdk_state_text, changed_at=now
        )

    def begin_veto_window(
        self,
        run_id: str,
        *,
        veto_id: str,
        deadline: float,
        sdk_state_text: str | None,
        now: float,
    ) -> tuple[Run, Veto]:
        """L3: atomically transition to ``veto_pending`` and open its one countdown.

        See ``Repository.begin_veto_window`` --- the run transition, the veto
        row, and the persisted SDK resume state all land in the same
        transaction, so a crash between them can never strand a
        ``veto_pending`` run without a veto row.
        """
        validate_transition(RunState.REHEARSED, RunState.VETO_PENDING)
        return self.repository.begin_veto_window(
            run_id,
            veto_id=veto_id,
            deadline=deadline,
            sdk_state_text=sdk_state_text,
            changed_at=now,
        )

    def reject_activation(self, run_id: str) -> Run:
        """A pre-activation re-check of the stored rehearsal failed: fail closed.

        Never reached by rehearsal itself (a failing rehearsal already lands
        in ``rehearsal_failed`` before a run is ever ``rehearsed``); this is
        the defense-in-depth path when the *stored* action plan or checks no
        longer match what a passing rehearsal recorded.
        """
        return self._transition(
            run_id,
            RunState.REHEARSED,
            RunState.REHEARSAL_FAILED,
            reason="activation_check_failed",
        )

    def record_system_denial(
        self, run_id: str, *, detail: dict[str, Any] | None = None
    ) -> tuple[Run, Verdict, Event]:
        """L1: the capability has no autonomy to activate a rehearsed plan at all."""
        run, event = self.repository._transition_with_event(
            run_id=run_id,
            expected_state=RunState.REHEARSED,
            new_state=RunState.DENIED,
            event_id=self._id_factory(),
            event_type=EventType.VETOED,
            weight=1.0,
            severity=None,
            evidence_key=f"approval:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python(detail or {}),
            demotion_levels=0,
            created_at=self._clock(),
            reason="system_denied_no_autonomy",
            verdict=(
                self._id_factory(),
                VerdictDecision.DENIED,
                VerdictActor.SYSTEM,
            ),
        )
        return run, self.repository.get_verdict(run_id), event

    def authorize_from_rehearsed(
        self, run_id: str, *, detail: dict[str, Any] | None = None
    ) -> tuple[Run, Verdict, Event]:
        """L4: the capability is trusted to authorize its own rehearsed plan."""
        run, event = self.repository._transition_with_event(
            run_id=run_id,
            expected_state=RunState.REHEARSED,
            new_state=RunState.AUTHORIZED,
            event_id=self._id_factory(),
            event_type=EventType.APPROVED,
            weight=1.0,
            severity=None,
            evidence_key=f"approval:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python(detail or {}),
            demotion_levels=0,
            created_at=self._clock(),
            reason="system_authorized_autonomous",
            verdict=(
                self._id_factory(),
                VerdictDecision.APPROVED,
                VerdictActor.SYSTEM,
            ),
        )
        return run, self.repository.get_verdict(run_id), event

    def authorize_after_veto_expiry(
        self, run_id: str, *, now: float
    ) -> tuple[Run, Verdict, Event] | None:
        """L3: atomically CAS an expired veto and authorize the run.

        ``None`` means the veto was already resolved (canceled or previously
        expired) --- a repeated call after the first successful resolution
        always returns ``None``, never authorizing the run a second time. See
        ``Repository.resolve_veto_with_event``: the veto CAS and the run's
        ``authorized`` transition/verdict/event land in the same transaction.
        """
        validate_transition(RunState.VETO_PENDING, RunState.AUTHORIZED)
        result = self.repository.resolve_veto_with_event(
            run_id,
            from_status=VetoStatus.PENDING,
            to_status=VetoStatus.EXPIRED,
            resolved_at=now,
            expected_state=RunState.VETO_PENDING,
            new_state=RunState.AUTHORIZED,
            event_id=self._id_factory(),
            event_type=EventType.APPROVED,
            weight=1.0,
            severity=None,
            evidence_key=f"approval:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python({}),
            demotion_levels=0,
            created_at=now,
            reason="veto_window_expired",
            verdict=(
                self._id_factory(),
                VerdictDecision.APPROVED,
                VerdictActor.SYSTEM,
            ),
        )
        if result is None:
            return None
        run, event = result
        return run, self.repository.get_verdict(run_id), event

    def start_execution(self, run_id: str) -> Run:
        return self._transition(
            run_id,
            RunState.AUTHORIZED,
            RunState.EXECUTING,
            reason="production_execution_started",
        )

    def expire_authorization(self, run_id: str) -> Run:
        return self._transition(
            run_id,
            RunState.AUTHORIZED,
            RunState.EXPIRED,
            reason="authorization_expired",
        )

    def record_shadow_result(
        self,
        *,
        bucket_id: int,
        playbook_version: int,
        heldout_artifact_digest: str,
        passed: bool,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Event:
        bucket = self.repository.get_bucket_by_id(bucket_id)
        if bucket.playbook_version != playbook_version:
            raise ConflictError(
                f"Bucket playbook is v{bucket.playbook_version}, not v{playbook_version}"
            )
        if bucket.activated_at is None or not bucket.playbook_digest:
            raise ConflictError("Shadow evidence requires an activated reviewed playbook")
        heldout = self.repository.get_demonstration_by_digest(heldout_artifact_digest)
        if heldout.bucket_id != bucket_id or heldout.role is not DemonstrationRole.HELDOUT:
            raise ConflictError(
                "Shadow evidence must reference a held-out demonstration for this bucket"
            )
        event_type = EventType.SHADOW_PASS if passed else EventType.SHADOW_FAIL
        severity = None if passed else Severity.MINOR
        return self._store_event(
            bucket_id=bucket_id,
            run_id=run_id,
            event_type=event_type,
            weight=1.0,
            severity=severity,
            evidence_key=f"shadow:{playbook_version}:{heldout_artifact_digest}",
            detail=detail,
        )

    def record_rehearsal_result(
        self,
        run_id: str,
        *,
        passed: bool,
        detail: dict[str, Any] | None = None,
        checks: Mapping[str, bool] | None = None,
        trace_ref: str | None = None,
        actions: Sequence[RecordedAction] | None = None,
    ) -> tuple[Run, Event]:
        """Record a rehearsal's pass/fail transition and twin_pass/twin_fail event.

        When ``actions`` is supplied (the twin-gated rehearsal path), also
        store the rehearsal's named checks, its exact ordered action plan,
        and the run's ``action_plan_hash`` --- all in the one transaction
        ``Repository.record_rehearsal`` owns. Omitting ``actions`` keeps the
        original transition-only behavior for callers that only exercise the
        run-state machine.
        """

        event_type = EventType.TWIN_PASS if passed else EventType.TWIN_FAIL
        severity = None if passed else Severity.MAJOR
        new_state = RunState.REHEARSED if passed else RunState.REHEARSAL_FAILED
        validate_transition(RunState.REHEARSING, new_state)
        if actions is None:
            return self.repository._transition_with_event(
                run_id=run_id,
                expected_state=RunState.REHEARSING,
                new_state=new_state,
                event_id=self._id_factory(),
                event_type=event_type,
                weight=self._twin_pass_weight if passed else 1.0,
                severity=severity,
                evidence_key=f"rehearsal:{run_id}",
                detail=_DETAIL_ADAPTER.validate_python(detail or {}),
                demotion_levels=self._demotion_for(severity),
                created_at=self._clock(),
                reason="rehearsal_passed" if passed else "rehearsal_failed",
            )
        if checks is None or trace_ref is None:
            raise ValueError("checks and trace_ref are required when actions are supplied")
        self._validate_rehearsal_claim(passed=passed, checks=checks, actions=actions)
        action_plan_hash = digest([action.model_dump(mode="json") for action in actions])
        return self.repository.record_rehearsal(
            run_id,
            passed=passed,
            checks=dict(checks),
            trace_ref=trace_ref,
            actions=actions,
            action_plan_hash=action_plan_hash,
            event_id=self._id_factory(),
            event_type=event_type,
            weight=self._twin_pass_weight if passed else 1.0,
            severity=severity,
            demotion_levels=self._demotion_for(severity),
            created_at=self._clock(),
        )

    @staticmethod
    def _validate_rehearsal_claim(
        *,
        passed: bool,
        checks: Mapping[str, bool],
        actions: Sequence[RecordedAction],
    ) -> None:
        """Reject internally inconsistent or non-replayable rehearsal claims."""

        if set(checks) != set(CHECK_NAMES):
            raise ValueError("rehearsal checks must contain exactly the six named invariants")
        if passed != all(checks.values()):
            raise ValueError("rehearsal passed flag must equal the conjunction of its checks")
        for expected_ordinal, action in enumerate(actions):
            if action.ordinal != expected_ordinal:
                raise ValueError("rehearsal action ordinals must be contiguous from zero")
            if action.arguments_digest != digest(action.arguments):
                raise ValueError("rehearsal action arguments digest does not match its arguments")
        commits = [action for action in actions if action.tool_name == "commit"]
        if passed and (
            not actions
            or len(commits) != 1
            or actions[-1].tool_name != "commit"
        ):
            raise ValueError("a passing rehearsal requires exactly one final commit action")

    def record_approval(
        self,
        run_id: str,
        *,
        attention: ApprovalAttention | str = ApprovalAttention.DEFAULT,
        with_edits: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> tuple[Run, Verdict, Event]:
        attention = ApprovalAttention(attention)
        run, event = self.repository._transition_with_event(
            run_id=run_id,
            expected_state=RunState.APPROVAL_PENDING,
            new_state=RunState.AUTHORIZED,
            event_id=self._id_factory(),
            event_type=(EventType.APPROVED_WITH_EDITS if with_edits else EventType.APPROVED),
            weight=self._approval_weights[attention],
            severity=None,
            evidence_key=f"approval:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python(
                {**(detail or {}), "attention": attention.value}
            ),
            demotion_levels=0,
            created_at=self._clock(),
            reason="human_approved",
            verdict=(
                self._id_factory(),
                VerdictDecision.APPROVED,
                VerdictActor.HUMAN,
            ),
        )
        return run, self.repository.get_verdict(run_id), event

    def record_denial(
        self, run_id: str, *, detail: dict[str, Any] | None = None
    ) -> tuple[Run, Verdict, Event]:
        run, event = self.repository._transition_with_event(
            run_id=run_id,
            expected_state=RunState.APPROVAL_PENDING,
            new_state=RunState.DENIED,
            event_id=self._id_factory(),
            event_type=EventType.VETOED,
            weight=1.0,
            severity=Severity.MINOR,
            evidence_key=f"approval:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python(detail or {}),
            demotion_levels=0,
            created_at=self._clock(),
            reason="human_denied",
            verdict=(
                self._id_factory(),
                VerdictDecision.DENIED,
                VerdictActor.HUMAN,
            ),
        )
        return run, self.repository.get_verdict(run_id), event

    def cancel_veto(self, run_id: str, *, now: float) -> tuple[Run, Event] | None:
        """L3: atomically CAS a pending veto to canceled and transition the run.

        ``None`` means the veto was already resolved (canceled or expired) by
        an earlier call --- the run is never touched a second time. See
        ``Repository.resolve_veto_with_event``: the veto CAS and the run's
        ``vetoed`` transition/event land in the same transaction, so a crash
        between them can never leave one without the other.
        """
        validate_transition(RunState.VETO_PENDING, RunState.VETOED)
        return self.repository.resolve_veto_with_event(
            run_id,
            from_status=VetoStatus.PENDING,
            to_status=VetoStatus.VETOED,
            resolved_at=now,
            expected_state=RunState.VETO_PENDING,
            new_state=RunState.VETOED,
            event_id=self._id_factory(),
            event_type=EventType.VETOED,
            weight=1.0,
            severity=Severity.MINOR,
            evidence_key=f"veto:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python({}),
            demotion_levels=0,
            created_at=now,
            reason="human_vetoed",
        )

    def record_verified_outcome(
        self,
        run_id: str,
        *,
        succeeded: bool,
        failure_cause: FailureCause | str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> tuple[Run, Event]:
        if succeeded and failure_cause is not None:
            raise ValueError("A successful outcome cannot have a failure cause")
        if not succeeded:
            failure_cause = FailureCause(failure_cause or FailureCause.EXECUTION_ERROR)
        severity = None if succeeded else FAILURE_SEVERITY[FailureCause(failure_cause)]
        new_state = RunState.SUCCEEDED if succeeded else RunState.FAILED
        event_type = EventType.RUN_SUCCESS if succeeded else EventType.RUN_FAILURE
        validate_transition(RunState.EXECUTING, new_state)
        trusted_detail = dict(detail or {})
        if {"trust_level", "failure_cause"} & trusted_detail.keys():
            raise ValueError("Outcome detail contains a reserved field")
        if failure_cause is not None:
            trusted_detail["failure_cause"] = FailureCause(failure_cause).value
        return self.repository._transition_with_event(
            run_id=run_id,
            expected_state=RunState.EXECUTING,
            new_state=new_state,
            event_id=self._id_factory(),
            event_type=event_type,
            weight=1.0,
            severity=severity,
            evidence_key=f"outcome:{run_id}",
            detail=_DETAIL_ADAPTER.validate_python(trusted_detail),
            demotion_levels=self._demotion_for(severity),
            created_at=self._clock(),
            reason="verified_success" if succeeded else "verified_failure",
            snapshot_trust_level=True,
        )

    def record_audit_result(
        self,
        *,
        bucket_id: int,
        audit_id: str,
        passed: bool,
        failure_cause: FailureCause | str | None = None,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Event:
        if passed and failure_cause is not None:
            raise ValueError("A passing audit cannot have a failure cause")
        if not passed:
            failure_cause = FailureCause(failure_cause or FailureCause.EXECUTION_ERROR)
        severity = None if passed else FAILURE_SEVERITY[FailureCause(failure_cause)]
        trusted_detail = dict(detail or {})
        if failure_cause is not None:
            trusted_detail["failure_cause"] = FailureCause(failure_cause).value
        return self._store_event(
            bucket_id=bucket_id,
            run_id=run_id,
            event_type=EventType.AUDIT_PASS if passed else EventType.AUDIT_FAIL,
            weight=1.0,
            severity=severity,
            evidence_key=f"audit:{audit_id}",
            detail=trusted_detail,
        )

    def _store_event(
        self,
        *,
        bucket_id: int,
        run_id: str | None,
        event_type: EventType,
        weight: float,
        severity: Severity | None,
        evidence_key: str,
        detail: dict[str, Any] | None,
    ) -> Event:
        return self.repository._record_event(
            event_id=self._id_factory(),
            bucket_id=bucket_id,
            run_id=run_id,
            event_type=event_type,
            weight=weight,
            severity=severity,
            evidence_key=evidence_key,
            detail=_DETAIL_ADAPTER.validate_python(detail or {}),
            demotion_levels=self._demotion_for(severity),
            created_at=self._clock(),
        )

    def _demotion_for(self, severity: Severity | None) -> int:
        return 0 if severity is None else self._severity_demotion[severity]


__all__ = [
    "ConflictError",
    "EvidenceConflictError",
    "InvalidTargetError",
    "InvalidTransitionError",
    "RunService",
    "UnknownBucketError",
    "UnknownEventTypeError",
    "UnknownRunError",
]
