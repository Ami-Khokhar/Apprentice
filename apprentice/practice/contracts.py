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


class DecisionAssessment(PracticeModel):
    response_excerpt: str = Field(min_length=1, max_length=500)
    interpretation: str = Field(min_length=1, max_length=800)
    strength: str = Field(min_length=1, max_length=500)
    risk: str = Field(min_length=1, max_length=500)
    evidence: tuple[EvidenceReference, ...] = Field(max_length=6)


class FacilitatorDecision(PracticeModel):
    action_kind: str | None = Field(max_length=80)
    requires_clarification: bool
    coaching_question: str | None = Field(max_length=500)
    assessment: DecisionAssessment

    @model_validator(mode="after")
    def validate_clarification(self) -> FacilitatorDecision:
        if self.requires_clarification:
            if self.action_kind is not None or not self.coaching_question:
                raise ValueError(
                    "a clarification must omit action_kind and include coaching_question"
                )
        elif self.action_kind is None:
            raise ValueError("an actionable response must include action_kind")
        return self


class PracticeTurn(PracticeModel):
    response: str = Field(min_length=1, max_length=8_000)
    action_kind: str | None = None
    coaching_question: str | None = None
    assessment: DecisionAssessment
    event_count: int = Field(ge=1)
    outcome: Literal["active", "recovered", "terminal_escalation"]


class FinalDebrief(PracticeModel):
    noticed: tuple[str, ...] = Field(min_length=1, max_length=6)
    missed: tuple[str, ...] = Field(min_length=1, max_length=6)
    strong_decisions: tuple[str, ...] = Field(min_length=1, max_length=6)
    risky_assumptions: tuple[str, ...] = Field(min_length=1, max_length=6)
    better_path: tuple[str, ...] = Field(min_length=1, max_length=8)
    expert_approach: str = Field(min_length=1, max_length=1_500)
    carry_forward: str = Field(min_length=1, max_length=500)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1, max_length=10)


class PracticeSession(PracticeModel):
    id: str
    profile: LearnerProfile
    scenario: ScenarioBlueprint
    incident_id: str
    world: dict[str, Any]
    runtime_kind: Literal["generated"] = "generated"
    generated_spec: GeneratedScenarioSpec
    turns: tuple[PracticeTurn, ...] = ()
    debrief: FinalDebrief | None = None
    created_at: float
    updated_at: float
