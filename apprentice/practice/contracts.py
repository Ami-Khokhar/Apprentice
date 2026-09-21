"""Typed contracts for the professional-practice flow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apprentice.incident.generated import GeneratedScenarioSpec


class PracticeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LearnerProfile(PracticeModel):
    field: str = Field(min_length=1, max_length=240)
    work_context: str | None = Field(default=None, max_length=4_000)
    difficulty_level: int = Field(default=1, ge=1, le=10)


class JudgmentProfileIdentity(PracticeModel):
    display_name: str = Field(default="Apprentice learner", min_length=1, max_length=120)
    headline: str = Field(default="", max_length=240)
    bio: str = Field(default="", max_length=2_000)
    updated_at: float | None = None


class JudgmentProfileCounts(PracticeModel):
    encountered: int = Field(ge=0)
    resolved: int = Field(ge=0)
    reviewed: int = Field(ge=0)
    active: int = Field(ge=0)


class JudgmentProfile(JudgmentProfileIdentity):
    counts: JudgmentProfileCounts


class ScenarioBlueprint(PracticeModel):
    """Learner-facing framing bound to one frozen deterministic world."""

    scenario_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    learner_role: str = Field(min_length=1, max_length=300)
    briefing: str = Field(min_length=1, max_length=2_000)
    how_it_developed: tuple[str, ...] = Field(min_length=1, max_length=5)
    immediate_constraints: tuple[str, ...] = Field(min_length=1, max_length=5)
    first_decision: str = Field(min_length=1, max_length=500)


class EvidenceReference(PracticeModel):
    source: Literal["event", "artifact", "metric", "action"]
    ref: str = Field(min_length=1, max_length=160)


class ClarificationReference(PracticeModel):
    source: Literal["scenario", "fact", "event", "artifact", "metric"]
    ref: str = Field(min_length=1, max_length=160)


class ClarificationResponse(PracticeModel):
    status: Literal["answered", "refused", "unavailable"]
    answer: str = Field(min_length=1, max_length=1_500)
    evidence: tuple[ClarificationReference, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def require_grounding_only_for_answers(self) -> ClarificationResponse:
        if self.status == "answered" and not self.evidence:
            raise ValueError("an answered clarification must cite observable evidence")
        if self.status != "answered" and self.evidence:
            raise ValueError("a non-answering clarification must not cite evidence")
        return self


class ClarificationExchange(PracticeModel):
    question: str = Field(min_length=1, max_length=2_000)
    response: ClarificationResponse


class DecisionAssessment(PracticeModel):
    disposition: Literal["accepted", "partially_effective", "needs_clarification", "unsafe"]
    response_excerpt: str = Field(min_length=1, max_length=500)
    interpretation: str = Field(min_length=1, max_length=800)
    recognized_intents: tuple[str, ...] = Field(max_length=6)
    strength: str = Field(min_length=1, max_length=500)
    risk: str | None = Field(max_length=500)
    evidence: tuple[EvidenceReference, ...] = Field(max_length=6)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_assessments(cls, value: Any) -> Any:
        if isinstance(value, dict) and "disposition" not in value:
            value = {"disposition": "partially_effective", **value}
        if isinstance(value, dict) and "recognized_intents" not in value:
            value = {"recognized_intents": (), **value}
        return value


class FacilitatorDecision(PracticeModel):
    action_kind: str | None = Field(max_length=80)
    prerequisite_override: bool
    override_justification: str | None = Field(max_length=500)
    requires_clarification: bool
    coaching_question: str | None = Field(max_length=500)
    assessment: DecisionAssessment

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_decisions(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = {
                "prerequisite_override": False,
                "override_justification": None,
                **value,
            }
        return value

    @model_validator(mode="after")
    def validate_clarification(self) -> FacilitatorDecision:
        if self.prerequisite_override and (
            self.action_kind is None or not self.override_justification
        ):
            raise ValueError("a prerequisite override needs an action and justification")
        if not self.prerequisite_override and self.override_justification is not None:
            raise ValueError("override justification requires a prerequisite override")
        if self.assessment.disposition == "needs_clarification":
            if not self.requires_clarification:
                raise ValueError("needs_clarification must set requires_clarification")
            if self.action_kind is not None or not self.coaching_question:
                raise ValueError(
                    "a clarification must omit action_kind and include coaching_question"
                )
        elif self.requires_clarification:
            raise ValueError("requires_clarification needs a clarification assessment")
        elif self.assessment.disposition == "unsafe":
            if self.action_kind is not None or self.prerequisite_override:
                raise ValueError("an unsafe response must not execute an action")
        return self


class PracticeTurn(PracticeModel):
    response: str = Field(min_length=1, max_length=8_000)
    action_kind: str | None = None
    coaching_question: str | None = None
    assessment: DecisionAssessment
    event_count: int = Field(ge=1)
    outcome: Literal["active", "recovered", "terminal_escalation"]


class PortfolioTurn(PracticeModel):
    response: str = Field(min_length=1, max_length=8_000)
    action_kind: str | None = Field(default=None, max_length=80)
    recognized_intents: tuple[str, ...] = Field(max_length=6)
    disposition: Literal["accepted", "partially_effective", "needs_clarification", "unsafe"]
    interpretation: str = Field(min_length=1, max_length=800)
    strength: str = Field(min_length=1, max_length=500)
    risk: str | None = Field(max_length=500)
    evidence: tuple[EvidenceReference, ...] = Field(max_length=6)
    coaching_question: str | None = Field(default=None, max_length=500)
    event_count: int = Field(ge=1)
    outcome: Literal["active", "recovered", "terminal_escalation"]


class FinalDebrief(PracticeModel):
    score: int | None = Field(ge=0, le=100)
    score_rationale: str | None = Field(min_length=1, max_length=500)
    noticed: tuple[str, ...] = Field(min_length=1, max_length=6)
    missed: tuple[str, ...] = Field(max_length=6)
    strong_decisions: tuple[str, ...] = Field(max_length=6)
    risky_assumptions: tuple[str, ...] = Field(max_length=6)
    better_path: tuple[str, ...] = Field(min_length=1, max_length=8)
    expert_approach: str = Field(min_length=1, max_length=1_500)
    carry_forward: str = Field(min_length=1, max_length=500)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_unscored_debriefs(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = {"score": None, "score_rationale": None, **value}
        return value

    @model_validator(mode="after")
    def keep_score_and_rationale_together(self) -> FinalDebrief:
        if (self.score is None) != (self.score_rationale is None):
            raise ValueError("score and score_rationale must both be present or absent")
        return self


class PracticeSession(PracticeModel):
    id: str
    profile: LearnerProfile
    scenario: ScenarioBlueprint
    incident_id: str
    world: dict[str, Any]
    runtime_kind: Literal["generated"] = "generated"
    generated_spec: GeneratedScenarioSpec
    turns: tuple[PracticeTurn, ...] = ()
    clarifications: tuple[ClarificationExchange, ...] = ()
    manually_stopped: bool = False
    debrief: FinalDebrief | None = None
    created_at: float
    updated_at: float


class PortfolioCase(PracticeModel):
    session_id: str
    title: str = Field(min_length=1, max_length=160)
    field: str = Field(min_length=1, max_length=240)
    work_context: str | None = Field(default=None, max_length=4_000)
    difficulty_level: int = Field(ge=1, le=10)
    learner_role: str = Field(min_length=1, max_length=300)
    briefing: str = Field(min_length=1, max_length=2_000)
    immediate_constraints: tuple[str, ...] = Field(min_length=1, max_length=5)
    first_decision: str = Field(min_length=1, max_length=500)
    status: Literal["active", "resolved", "reviewed"]
    outcome: Literal["active", "recovered", "terminal_escalation"]
    turns: tuple[PortfolioTurn, ...]
    clarifications: tuple[ClarificationExchange, ...]
    evidence: tuple[EvidenceReference, ...]
    score: int | None = Field(default=None, ge=0, le=100)
    debrief: FinalDebrief | None = None
    created_at: float
    updated_at: float
