from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class FrozenDict(dict[Any, Any]):
    """A serializer-friendly dictionary that rejects mutation."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Frozen model containers cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def freeze_containers(self) -> FrozenModel:
        for field_name in type(self).model_fields:
            object.__setattr__(self, field_name, _deep_freeze(getattr(self, field_name)))
        return self


class BucketKind(StrEnum):
    PLAYBOOK = "playbook"
    TOOL = "tool"


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class BucketOrigin(StrEnum):
    DEMONSTRATED = "demonstrated"
    SELF_AUTHORED = "self_authored"


class DemonstrationRole(StrEnum):
    TRAINING = "training"
    HELDOUT = "heldout"


class RunState(StrEnum):
    CREATED = "created"
    REHEARSING = "rehearsing"
    REHEARSAL_FAILED = "rehearsal_failed"
    REHEARSED = "rehearsed"
    APPROVAL_PENDING = "approval_pending"
    VETO_PENDING = "veto_pending"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VETOED = "vetoed"
    EXPIRED = "expired"


class EventType(StrEnum):
    SHADOW_PASS = "shadow_pass"
    SHADOW_FAIL = "shadow_fail"
    TWIN_PASS = "twin_pass"
    TWIN_FAIL = "twin_fail"
    APPROVED = "approved"
    APPROVED_WITH_EDITS = "approved_with_edits"
    VETOED = "vetoed"
    RUN_SUCCESS = "run_success"
    RUN_FAILURE = "run_failure"
    AUDIT_PASS = "audit_pass"
    AUDIT_FAIL = "audit_fail"


class Severity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class FailureCause(StrEnum):
    EXECUTION_ERROR = "execution_error"
    PLAN_MISMATCH = "plan_mismatch"
    UNSAFE_SIDE_EFFECT = "unsafe_side_effect"


class ApprovalAttention(StrEnum):
    RUBBER_STAMP = "rubber_stamp"
    DEFAULT = "default"
    ENGAGED = "engaged"


class VerdictDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class VerdictActor(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"


class Bucket(FrozenModel):
    id: int
    name: str
    kind: BucketKind
    risk_class: RiskClass
    level: int = Field(ge=0, le=4)
    origin: BucketOrigin
    playbook_version: int = Field(ge=1)
    playbook_digest: str
    target_base_url: str
    allowed_hosts: tuple[str, ...]
    tool_versions: dict[str, str]
    toolset_digest: str
    created_at: float
    last_activity_at: float
    last_decay_at: float | None = None
    activated_at: float | None = None

    @model_validator(mode="after")
    def critical_cap(self) -> Bucket:
        if self.risk_class is RiskClass.CRITICAL and self.level > 3:
            raise ValueError("Critical-risk capabilities are capped at L3")
        return self


class Demonstration(FrozenModel):
    id: str
    bucket_id: int
    artifact_ref: str
    artifact_digest: str
    role: DemonstrationRole
    created_at: float


class ReviewedPlaybook(FrozenModel):
    bucket_id: int
    version: int = Field(ge=1)
    content: JsonValue
    digest: str
    reviewed_at: float


_ACTION_EFFECT: dict[str, str] = {
    "navigate": "observe",
    "fill": "prepare",
    "upload": "prepare",
    "click": "prepare",
    "commit": "commit",
}


class RecordedAction(FrozenModel):
    """One canonical recorded action (plan §6.3); the run_actions row contract."""

    ordinal: int = Field(ge=0)
    tool_name: Literal["navigate", "fill", "upload", "click", "commit"]
    arguments: dict[str, JsonValue]
    arguments_digest: str
    effect: Literal["observe", "prepare", "commit"]

    @model_validator(mode="after")
    def _known_action_effect_combination(self) -> RecordedAction:
        if _ACTION_EFFECT[self.tool_name] != self.effect:
            raise ValueError(
                f"unknown action/effect combination: {self.tool_name}/{self.effect}"
            )
        return self


class Run(FrozenModel):
    id: str
    bucket_id: int
    playbook_version: int
    playbook_digest: str
    inputs: dict[str, JsonValue]
    input_digest: str
    target_base_url: str
    allowed_hosts: tuple[str, ...]
    tool_versions: dict[str, str]
    toolset_digest: str
    state: RunState
    action_plan_hash: str | None = None
    sdk_state_ref: str | None = None
    created_at: float
    updated_at: float


class RunTransition(FrozenModel):
    id: int
    run_id: str
    from_state: RunState | None
    to_state: RunState
    reason: str = Field(min_length=1)
    created_at: float


class Verdict(FrozenModel):
    id: str
    run_id: str
    decision: VerdictDecision
    decided_by: VerdictActor
    created_at: float


class VetoStatus(StrEnum):
    PENDING = "pending"
    VETOED = "vetoed"
    AUTHORIZED = "authorized"
    EXPIRED = "expired"


class Veto(FrozenModel):
    """One run's L3 veto countdown: exactly one row per run (plan §6.4)."""

    id: str
    run_id: str
    deadline: float
    status: VetoStatus
    resolved_at: float | None = None


class Event(FrozenModel):
    id: str
    bucket_id: int
    run_id: str | None
    type: EventType
    weight: float = Field(gt=0)
    severity: Severity | None
    evidence_key: str
    detail: dict[str, JsonValue]
    created_at: float
    playbook_version: int = Field(default=1, ge=1)


class PromotionEvaluation(FrozenModel):
    eligible: bool
    target_level: int | None
    score: float
    unmet: tuple[str, ...] = ()


class ErrorDetail(FrozenModel):
    code: str
    message: str


class ErrorResponse(FrozenModel):
    error: ErrorDetail
