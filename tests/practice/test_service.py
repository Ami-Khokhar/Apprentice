from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from apprentice.database import SQLiteDatabase
from apprentice.incident.generated import GeneratedScenarioSpec
from apprentice.practice.codex_runner import AgentSpec, CodexOutputError, CodexStructuredRunner
from apprentice.practice.contracts import (
    ClarificationReference,
    ClarificationResponse,
    DecisionAssessment,
    EvidenceReference,
    FacilitatorDecision,
    FinalDebrief,
    LearnerProfile,
    PracticeSession,
)
from apprentice.practice.service import (
    DEFAULT_PRACTICE_MODEL,
    InvalidPracticeResponseError,
    MemorySessionStore,
    PracticeNotReadyForDebriefError,
    PracticeService,
    PracticeSessionNotFoundError,
)
from tests.incident.test_generated import scenario_data


class FakeRunner:
    def __init__(self, outputs: Iterable[BaseModel | Exception]) -> None:
        self.outputs = iter(outputs)
        self.calls: list[tuple[AgentSpec, str]] = []

    def __call__(self, agent: AgentSpec, prompt: str) -> BaseModel:
        self.calls.append((agent, prompt))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, GeneratedScenarioSpec):
            output = output.model_copy(
                update={"generation_nonce": json.loads(prompt)["generation_nonce"]}
            )
        return output


class FakeTraceObservation:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.record.setdefault("updates", []).append(
            {"output": output, "metadata": dict(metadata or {})}
        )


class FakeTracer:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.stack: list[str] = []

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        session_id: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[FakeTraceObservation]:
        with self._record(
            "span", name, input, {"session_id": session_id, **(metadata or {})}
        ) as record:
            yield FakeTraceObservation(record)

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        output_schema: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[FakeTraceObservation]:
        with self._record(
            "generation",
            name,
            input,
            {"model": model, "output_schema": output_schema, **(metadata or {})},
        ) as record:
            yield FakeTraceObservation(record)

    @contextmanager
    def _record(
        self, kind: str, name: str, input: object, metadata: Mapping[str, object]
    ) -> Iterator[dict[str, Any]]:
        record = {
            "kind": kind,
            "name": name,
            "parent": self.stack[-1] if self.stack else None,
            "input": input,
            "metadata": dict(metadata),
        }
        self.records.append(record)
        self.stack.append(name)
        try:
            yield record
        finally:
            self.stack.pop()


def generated_spec(*, distinct: bool = False) -> GeneratedScenarioSpec:
    data = scenario_data()
    if distinct:
        data.update(
            {
                "scenario_id": "generated-warehouse-robot-002",
                "core_challenge": (
                    "Keep warehouse staff safe while autonomous carts begin taking conflicting "
                    "routes through an occupied loading zone."
                ),
                "failure_mechanism": (
                    "A stale map version assigns two robot fleets incompatible right-of-way rules."
                ),
                "decision_tradeoff": (
                    "An emergency stop protects people but blocks urgent medical shipments; "
                    "continued operation risks a collision."
                ),
            }
        )
    return GeneratedScenarioSpec.model_validate(data)


def decision(
    action: str | None,
    *,
    excerpt: str,
    clarification: bool = False,
    evidence: tuple[EvidenceReference, ...] = (),
    disposition: str | None = None,
    risk: str | None = "The consequence must still be checked against evidence.",
    prerequisite_override: bool = False,
    override_justification: str | None = None,
) -> FacilitatorDecision:
    return FacilitatorDecision(
        action_kind=action,
        prerequisite_override=prerequisite_override,
        override_justification=override_justification,
        requires_clarification=clarification,
        coaching_question="Which concrete step comes first?" if clarification else None,
        assessment=DecisionAssessment(
            disposition=disposition or (
                "needs_clarification" if clarification else "partially_effective"
            ),
            response_excerpt=excerpt,
            interpretation="You identified a concrete next step.",
            recognized_intents=("Take the proposed immediate step.",),
            strength="The response establishes a testable action.",
            risk=risk,
            evidence=evidence,
        ),
    )


def debrief() -> FinalDebrief:
    return FinalDebrief(
        score=82,
        score_rationale="The response contained the issue and verified the correction.",
        noticed=("The forecast required verification.",),
        missed=("The partner deadline added pressure.",),
        strong_decisions=("You inspected the pipeline.",),
        risky_assumptions=("The anomaly could have been accepted at face value.",),
        better_path=("Inspect evidence.", "Correct the source.", "Validate the forecast."),
        expert_approach="Verify the aggregation, correct it, and measure the remaining risk.",
        carry_forward="Pair urgent action with a falsifiable evidence check.",
        evidence=(EvidenceReference(source="metric", ref="stockout-risk"),),
    )


def clarification(
    answer: str = "The partner assets go live in six hours.",
    *,
    status: Literal["answered", "refused", "unavailable"] = "answered",
    evidence: tuple[ClarificationReference, ...] = (
        ClarificationReference(source="fact", ref="launch-window"),
    ),
) -> ClarificationResponse:
    return ClarificationResponse(status=status, answer=answer, evidence=evidence)


def service_with_db(path: Path, runner: FakeRunner) -> tuple[PracticeService, SQLiteDatabase]:
    database = SQLiteDatabase(path)
    return PracticeService(database=database, runner=runner), database


def test_service_defaults_to_locally_authenticated_codex_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    service = PracticeService()

    assert isinstance(service._runner, CodexStructuredRunner)


def test_start_uses_one_terra_generator_without_authored_catalog() -> None:
    runner = FakeRunner([generated_spec()])
    service = PracticeService(runner=runner)

    session = service.start("supply chain analyst", "I plan regional launches.")

    assert session.world["scenario_fingerprint"] == session.generated_spec.fingerprint
    assert len(runner.calls) == 1
    agent, raw_prompt = runner.calls[0]
    assert agent.name == "dojo-scenario-generator"
    assert agent.model == DEFAULT_PRACTICE_MODEL == "gpt-5.6-terra"
    assert agent.output_type is GeneratedScenarioSpec
    assert "authored_catalog" not in raw_prompt
    assert json.loads(raw_prompt)["prior_situations_to_avoid"] == []


def test_start_persists_and_prompts_for_selected_difficulty() -> None:
    runner = FakeRunner([generated_spec()])
    service = PracticeService(runner=runner)

    session = service.start("software operations", difficulty_level=10)

    assert session.profile.difficulty_level == 10
    calibration = json.loads(runner.calls[0][1])["difficulty_calibration"]
    assert calibration["selected_level"] == 10
    assert "CTO-level" in calibration["learner_experience"]
    assert "Do not test expertise above the selected level" in calibration["instruction"]


def test_generation_prompt_keeps_language_clear_without_revealing_the_decision() -> None:
    runner = FakeRunner([generated_spec()])
    service = PracticeService(runner=runner)

    service.start("software operations", difficulty_level=10)

    prompt = json.loads(runner.calls[0][1])
    readability = prompt["readability_contract"]
    assert "immediate problem in the first sentence" in readability["briefing"]
    assert "one fact in each sentence" in readability["briefing"]
    assert "define it briefly" in readability["terminology"]
    assert "Do not combine multiple questions" in readability["first_decision"]
    assert "not use harder English" in readability["difficulty_independence"]
    assert "not from difficult wording" in prompt["difficulty_calibration"]["instruction"]


@pytest.mark.parametrize("difficulty_level", [0, 11])
def test_start_rejects_difficulty_outside_scale(difficulty_level: int) -> None:
    runner = FakeRunner([generated_spec()])
    service = PracticeService(runner=runner)

    with pytest.raises(ValidationError):
        service.start("software operations", difficulty_level=difficulty_level)

    assert runner.calls == []


def test_legacy_learner_profile_defaults_to_beginner_difficulty() -> None:
    profile = LearnerProfile.model_validate({"field": "software operations"})

    assert profile.difficulty_level == 1


def test_duplicate_generation_retries_for_field_when_work_context_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dojo.sqlite3"
    first_runner = FakeRunner([generated_spec()])
    first, database = service_with_db(path, first_runner)
    first.start(" Supply   Chain Analyst ", "Regional LAUNCHES")

    second_runner = FakeRunner([generated_spec(), generated_spec(distinct=True)])
    restarted = PracticeService(database=database, runner=second_runner)
    session = restarted.start("supply chain analyst", "I now plan store openings")

    assert session.generated_spec is not None
    assert session.generated_spec.scenario_id == "generated-warehouse-robot-002"
    assert len(second_runner.calls) == 2
    first_retry_prompt = json.loads(second_runner.calls[0][1])
    assert first_retry_prompt["prior_situations_to_avoid"][0]["fingerprint"]
    assert json.loads(second_runner.calls[1][1])["attempt"] == 2


def test_generated_store_ignores_pre_dojo_sessions(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "dojo.sqlite3")
    with database.transaction(write=True) as connection:
        connection.execute(
            """
            INSERT INTO practice_sessions(
              id, incident_id, session_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("old", "old-incident", '{"id":"old"}', 1.0, 1.0),
        )

    service = PracticeService(database=database, runner=FakeRunner([generated_spec()]))

    assert service.start("analyst").generated_spec.scenario_id
    with pytest.raises(PracticeSessionNotFoundError):
        service.get_session("old")


def test_duplicate_exhaustion_fails_closed_without_saving_another_session() -> None:
    store = MemorySessionStore()
    first = PracticeService(runner=FakeRunner([generated_spec()]), store=store)
    first.start("analyst")
    runner = FakeRunner([generated_spec(), generated_spec(), generated_spec()])
    service = PracticeService(runner=runner, store=store)

    with pytest.raises(InvalidPracticeResponseError, match="different situation"):
        service.start("ANALYST")

    assert len(runner.calls) == 3
    assert len(store._sessions) == 1


def test_invalid_generated_draft_retries_then_valid_draft_succeeds() -> None:
    runner = FakeRunner(
        [CodexOutputError("invalid generated draft"), generated_spec(distinct=True)]
    )
    service = PracticeService(runner=runner)

    session = service.start("analyst")

    assert session.generated_spec is not None
    assert session.generated_spec.scenario_id == "generated-warehouse-robot-002"
    assert len(runner.calls) == 2
    contract = json.loads(runner.calls[1][1])["validation_contract"]
    assert contract["initially_enabled_actions_minimum"] == 2
    assert "before the first terminal escalation" in contract["success_path"]


def test_invalid_generated_drafts_exhaust_retries_without_saving() -> None:
    store = MemorySessionStore()
    errors = [CodexOutputError(f"invalid draft {index}") for index in range(3)]
    runner = FakeRunner(errors)
    service = PracticeService(runner=runner, store=store)

    with pytest.raises(CodexOutputError, match="invalid draft 2"):
        service.start("analyst")

    assert len(runner.calls) == 3
    assert store._sessions == {}


def test_generated_session_restores_and_continues_after_service_restart(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                "inspect-pipeline",
                excerpt="Inspect the pipeline",
                evidence=(EvidenceReference(source="action", ref="inspect-pipeline"),),
            ),
            decision(
                "correct-and-reforecast",
                excerpt="Correct and reforecast",
                evidence=(EvidenceReference(source="artifact", ref="transform-audit"),),
            ),
            debrief(),
        ]
    )
    service, database = service_with_db(tmp_path / "dojo.sqlite3", runner)
    session = service.start("analyst")
    assert session.model_dump(mode="json")["runtime_kind"] == "generated"
    inspected = service.respond(session.id, "Inspect the pipeline first.")
    assert inspected.world["completed"] is False

    restarted = PracticeService(database=database, runner=runner)
    restored = restarted.get_session(session.id)
    assert restored.world["completed_actions"] == ["inspect-pipeline"]
    recovered = restarted.respond(session.id, "Correct and reforecast now.")

    assert recovered.world["completed"] is True
    assert recovered.world["outcome"] == "recovered"
    assert restarted.get_debrief(session.id).carry_forward.startswith("Pair urgent")


def test_clarification_records_turn_without_advancing_generated_world() -> None:
    runner = FakeRunner([generated_spec(), decision(None, excerpt="handle it", clarification=True)])
    service = PracticeService(runner=runner)
    session = service.start("analyst")

    updated = service.respond(session.id, "handle it")

    assert updated.world["sim_time"] == 0
    assert updated.turns[0].action_kind is None


def test_situation_clarification_is_grounded_persisted_and_never_advances_world() -> None:
    runner = FakeRunner([generated_spec(), clarification()])
    service = PracticeService(runner=runner)
    session = service.start("analyst")
    before_world = session.world.copy()

    updated = service.clarify(session.id, " When do the partner assets go live? ")

    assert updated.world == before_world
    assert updated.turns == ()
    assert updated.clarifications[0].question == "When do the partner assets go live?"
    assert updated.clarifications[0].response.answer == (
        "The partner assets go live in six hours."
    )
    assert service.get_session(session.id).clarifications == updated.clarifications
    agent, raw_prompt = runner.calls[-1]
    prompt = json.loads(raw_prompt)
    assert agent.name == "dojo-situation-clarifier"
    assert agent.output_type is ClarificationResponse
    assert "transition_catalog" not in prompt
    assert "canonical_world" not in prompt
    assert "generated_spec" not in raw_prompt
    assert "failure_mechanism" not in raw_prompt
    assert "success_requirements" not in raw_prompt
    assert "rubric" not in raw_prompt
    assert "duplicate-orders" not in raw_prompt
    assert "transform-audit" not in raw_prompt
    assert prompt["allowed_evidence"]["fact"] == ["launch-window"]


def test_situation_clarification_rejects_unobservable_evidence_without_saving() -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            clarification(
                "The duplicate was caused by a timezone conversion.",
                evidence=(
                    ClarificationReference(source="fact", ref="duplicate-orders"),
                ),
            ),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("analyst")

    with pytest.raises(InvalidPracticeResponseError, match="unknown fact"):
        service.clarify(session.id, "Why did the forecast jump?")

    restored = service.get_session(session.id)
    assert restored.clarifications == ()
    assert restored.world == session.world


def test_situation_clarification_uses_safe_server_copy_for_refused_hint() -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            clarification(
                "Secret hint: inspect the pipeline first.",
                status="refused",
                evidence=(),
            ),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("analyst")

    updated = service.clarify(session.id, "What should I do first?")

    answer = updated.clarifications[0].response.answer
    assert "inspect" not in answer.casefold()
    assert "cannot suggest" in answer


def test_situation_clarification_is_unavailable_after_session_ends() -> None:
    runner = FakeRunner([generated_spec()])
    service = PracticeService(runner=runner)
    session = service.start("analyst")
    store = service._store
    store.save(session.model_copy(update={"manually_stopped": True}))

    with pytest.raises(InvalidPracticeResponseError, match="already been ended"):
        service.clarify(session.id, "What was the current risk?")

    assert len(runner.calls) == 1


def test_reasonable_rollback_can_bypass_preferred_order_without_invented_criticism() -> None:
    response = (
        "Revert the implicated overnight upload change to yesterday's version, then validate "
        "the release."
    )
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                "correct-and-reforecast",
                excerpt="Revert the implicated overnight upload change",
                disposition="accepted",
                risk=None,
                prerequisite_override=True,
                override_justification=(
                    "The learner identified the implicated recent change and can restore the "
                    "last known-good state without waiting for the preferred inspection."
                ),
                evidence=(EvidenceReference(source="action", ref="correct-and-reforecast"),),
            ),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("software operations")

    updated = service.respond(session.id, response)

    assert updated.turns[0].assessment.disposition == "accepted"
    assert updated.turns[0].assessment.risk is None
    assert updated.world["completed_actions"] == ["correct-and-reforecast"]
    assert updated.world["outcome"] == "recovered"
    prompt = json.loads(runner.calls[-1][1])
    assert "canonical_world" not in prompt
    assert "generated_spec" not in json.dumps(prompt)
    assert all("prerequisites" not in action for action in prompt["transition_catalog"])
    assert prompt["observable_world"]["artifacts"] == [
        {
            "id": "forecast-dashboard",
            "title": "Forecast dashboard",
            "kind": "dashboard",
            "content": "The forecast is 41% above the trailing four-week range.",
            "visible_at_start": True,
        }
    ]


def test_defensible_unmodelled_action_is_recorded_without_mutating_the_world() -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                None,
                excerpt="Route the release through a manual approval",
                disposition="accepted",
                risk=None,
                evidence=(EvidenceReference(source="metric", ref="stockout-risk"),),
            ),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("software operations")

    updated = service.respond(
        session.id, "Route the release through a manual approval until this is understood."
    )

    assert updated.turns[0].assessment.disposition == "accepted"
    assert updated.turns[0].action_kind is None
    assert updated.world["completed_actions"] == []
    assert updated.world["sim_time"] == 0


def test_disabled_transition_needs_an_explicit_feasibility_override() -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                "correct-and-reforecast",
                excerpt="Correct it now",
                disposition="accepted",
                risk=None,
                evidence=(EvidenceReference(source="action", ref="correct-and-reforecast"),),
            ),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("software operations")

    with pytest.raises(InvalidPracticeResponseError, match="unmet dependencies"):
        service.respond(session.id, "Correct it now")

    assert service.get_session(session.id).world["completed_actions"] == []


def test_facilitator_prompt_requires_real_world_adjudication_and_multiple_paths() -> None:
    runner = FakeRunner([generated_spec()])
    service = PracticeService(runner=runner)
    session = service.start("software operations")

    generator_instructions = runner.calls[0][0].instructions
    assert "multiple defensible response paths" in generator_instructions
    assert "observable outcomes" in generator_instructions
    facilitator = service._facilitator_agent()
    assert "not an answer-key matcher" in facilitator.instructions
    assert "Never invent criticism" in facilitator.instructions
    prompt = json.loads(service._facilitator_prompt(session, "Roll back the change."))
    assert "observable_world" in prompt
    assert "transition_catalog" in prompt
    assert "canonical_world" not in prompt


def test_generated_action_and_evidence_validation_fail_before_mutation() -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                "correct-and-reforecast",
                excerpt="Correct now",
                evidence=(EvidenceReference(source="artifact", ref="invented"),),
            ),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("analyst")

    with pytest.raises(InvalidPracticeResponseError):
        service.respond(session.id, "Correct now")

    assert service.get_session(session.id).world["completed_actions"] == []


def test_debrief_requires_a_concluded_generated_world() -> None:
    service = PracticeService(runner=FakeRunner([generated_spec()]))
    session = service.start("analyst")

    with pytest.raises(PracticeNotReadyForDebriefError):
        service.get_debrief(session.id)


def test_manual_stop_requires_a_turn_then_freezes_and_debriefs_current_state() -> None:
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                "inspect-pipeline",
                excerpt="Inspect the pipeline",
                evidence=(EvidenceReference(source="action", ref="inspect-pipeline"),),
            ),
            debrief(),
        ]
    )
    service = PracticeService(runner=runner)
    session = service.start("analyst")

    with pytest.raises(InvalidPracticeResponseError, match="at least one decision"):
        service.stop(session.id)

    progressed = service.respond(session.id, "Inspect the pipeline first.")
    stopped = service.stop(session.id)

    assert stopped.manually_stopped is True
    assert stopped.world == progressed.world
    with pytest.raises(InvalidPracticeResponseError, match="already been ended"):
        service.respond(session.id, "Correct and reforecast now.")

    result = service.get_debrief(session.id)
    assert result.score == 82
    prompt = json.loads(runner.calls[-1][1])
    assert prompt["ending_reason"] == "manual_stop"
    assert prompt["scoring_evidence"]["action"] == ["inspect-pipeline"]
    assert "correct-and-reforecast" not in prompt["scoring_evidence"]["action"]
    assert "available or future actions earn no credit" in prompt["scoring_contract"].lower()


def test_legacy_session_and_debrief_load_without_manual_stop_or_score() -> None:
    session = PracticeSession.model_validate(
        {
            **PracticeService(runner=FakeRunner([generated_spec()]))
            .start("analyst")
            .model_dump(
                mode="json",
                exclude={"clarifications", "manually_stopped", "debrief"},
            ),
            "debrief": {
                key: value
                for key, value in debrief().model_dump(mode="json").items()
                if key not in {"score", "score_rationale"}
            },
        }
    )

    assert session.manually_stopped is False
    assert session.clarifications == ()
    assert session.debrief is not None
    assert session.debrief.score is None
    assert session.debrief.score_rationale is None


def test_tracing_groups_operations_and_records_model_and_deterministic_results() -> None:
    tracer = FakeTracer()
    runner = FakeRunner(
        [
            CodexOutputError("invalid draft"),
            generated_spec(),
            decision(
                "inspect-pipeline",
                excerpt="Inspect the pipeline",
                evidence=(EvidenceReference(source="action", ref="inspect-pipeline"),),
            ),
            debrief(),
        ]
    )
    service = PracticeService(runner=runner, tracer=tracer)

    session = service.start("analyst")
    service.respond(session.id, "Inspect the pipeline first.")
    service.stop(session.id)
    result = service.get_debrief(session.id)

    roots = [record for record in tracer.records if record["parent"] is None]
    assert [record["name"] for record in roots] == [
        "practice.start",
        "practice.respond",
        "practice.stop",
        "practice.debrief",
    ]
    assert {record["metadata"]["session_id"] for record in roots} == {session.id}

    generations = [record for record in tracer.records if record["kind"] == "generation"]
    assert [record["parent"] for record in generations] == [
        "practice.start",
        "practice.start",
        "practice.respond",
        "practice.debrief",
    ]
    assert generations[0]["metadata"]["attempt"] == 1
    assert generations[1]["metadata"]["attempt"] == 2
    assert generations[1]["metadata"]["retry_reason"] == "invalid_structured_output"
    assert generations[2]["metadata"]["agent_name"] == "dojo-decision-facilitator"
    assert generations[2]["metadata"]["output_schema"] == "FacilitatorDecision"

    response_output = roots[1]["updates"][0]["output"]
    assert response_output["selected_action"] == "inspect-pipeline"
    assert response_output["world_outcome"] == "active"
    assert isinstance(response_output["metric_delta"], dict)
    debrief_output = roots[3]["updates"][0]
    assert debrief_output["metadata"]["score"] == 82
    assert debrief_output["output"].score == result.score == 82
