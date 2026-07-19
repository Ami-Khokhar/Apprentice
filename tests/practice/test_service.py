from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest
from pydantic import BaseModel

from apprentice.database import SQLiteDatabase
from apprentice.incident.generated import GeneratedScenarioSpec
from apprentice.practice.codex_runner import AgentSpec, CodexOutputError, CodexStructuredRunner
from apprentice.practice.contracts import (
    DecisionAssessment,
    EvidenceReference,
    FacilitatorDecision,
    FinalDebrief,
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
) -> FacilitatorDecision:
    return FacilitatorDecision(
        action_kind=action,
        requires_clarification=clarification,
        coaching_question="Which concrete step comes first?" if clarification else None,
        assessment=DecisionAssessment(
            response_excerpt=excerpt,
            interpretation="You identified a concrete next step.",
            strength="The response establishes a testable action.",
            risk="The consequence must still be checked against evidence.",
            evidence=evidence,
        ),
    )


def debrief() -> FinalDebrief:
    return FinalDebrief(
        noticed=("The forecast required verification.",),
        missed=("The partner deadline added pressure.",),
        strong_decisions=("You inspected the pipeline.",),
        risky_assumptions=("The anomaly could have been accepted at face value.",),
        better_path=("Inspect evidence.", "Correct the source.", "Validate the forecast."),
        expert_approach="Verify the aggregation, correct it, and measure the remaining risk.",
        carry_forward="Pair urgent action with a falsifiable evidence check.",
        evidence=(EvidenceReference(source="metric", ref="stockout-risk"),),
    )


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
