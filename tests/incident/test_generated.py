from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from apprentice.incident.generated import (
    GeneratedScenarioError,
    GeneratedScenarioRuntime,
    GeneratedScenarioSpec,
    is_near_duplicate,
    novelty_similarity,
)


def scenario_data() -> dict[str, object]:
    return {
        "scenario_id": "generated-forecast-drift-001",
        "generation_nonce": "request-9af85c11",
        "title": "Demand forecast drift before launch",
        "learner_role": "Supply-chain analyst",
        "setting": "A regional launch is six hours away and replenishment is already moving.",
        "core_challenge": (
            "Decide whether to pause a launch while demand forecasts disagree with recent orders."
        ),
        "failure_mechanism": (
            "A timezone conversion duplicated one region's orders in the training aggregation."
        ),
        "decision_tradeoff": (
            "Pausing protects inventory but breaks a partner commitment; "
            "continuing risks stockouts."
        ),
        "briefing": "The launch forecast jumped 41% after the final planning run.",
        "first_decision": "What do you do in the next fifteen minutes?",
        "facts": [
            {
                "id": "launch-window",
                "statement": "Partner launch assets go live in six hours.",
                "source": "Launch plan",
                "known_at_start": True,
            },
            {
                "id": "duplicate-orders",
                "statement": "One region's orders were aggregated twice.",
                "source": "Transformation audit",
                "known_at_start": False,
            },
        ],
        "timeline": [
            {
                "minutes_before_start": 180,
                "title": "Orders imported",
                "detail": "The regional export entered the planning pipeline.",
            },
            {
                "minutes_before_start": 15,
                "title": "Forecast published",
                "detail": "The final forecast rose outside its historical band.",
            },
        ],
        "metrics": [
            {
                "id": "stockout-risk",
                "label": "Stockout risk",
                "unit": "percent",
                "initial": 55,
                "minimum": 0,
                "maximum": 100,
                "precision": 1,
            }
        ],
        "artifacts": [
            {
                "id": "forecast-dashboard",
                "title": "Forecast dashboard",
                "kind": "dashboard",
                "content": "The forecast is 41% above the trailing four-week range.",
                "visible_at_start": True,
            },
            {
                "id": "transform-audit",
                "title": "Transformation audit",
                "kind": "audit",
                "content": "The EU order partition appears twice after timezone normalization.",
                "visible_at_start": False,
            },
        ],
        "actions": [
            {
                "id": "inspect-pipeline",
                "label": "Inspect the planning pipeline",
                "description": "Trace source partitions and compare them with the published run.",
                "risk": "low",
                "prerequisites": [],
                "metric_effects": [],
                "reveals_artifacts": ["transform-audit"],
                "advances_minutes": 5,
            },
            {
                "id": "correct-and-reforecast",
                "label": "Correct and reforecast",
                "description": "Remove the duplicate partition and publish a validated forecast.",
                "risk": "medium",
                "prerequisites": ["inspect-pipeline"],
                "metric_effects": [
                    {"metric_id": "stockout-risk", "operation": "set", "value": 10}
                ],
                "reveals_artifacts": [],
                "advances_minutes": 5,
            },
        ],
        "timed_escalations": [
            {
                "id": "partner-asks-for-commitment",
                "at_minute": 7,
                "title": "Partner requests confirmation",
                "detail": "The partner asks whether the launch allocation is final.",
                "metric_effects": [
                    {"metric_id": "stockout-risk", "operation": "add", "value": 10}
                ],
                "reveals_artifacts": [],
                "terminal": False,
            },
            {
                "id": "allocation-locks",
                "at_minute": 30,
                "title": "Allocation locks",
                "detail": "Warehouse allocation locks while the forecast remains unreliable.",
                "metric_effects": [],
                "reveals_artifacts": [],
                "terminal": True,
            },
        ],
        "success_requirements": [
            {
                "id": "corrected",
                "description": "Publish a corrected forecast.",
                "kind": "action_completed",
                "ref": "correct-and-reforecast",
                "threshold": None,
            },
            {
                "id": "risk-controlled",
                "description": "Bring stockout risk to twenty percent or less.",
                "kind": "metric_at_most",
                "ref": "stockout-risk",
                "threshold": 20,
            },
        ],
        "rubric": [
            {
                "id": "evidence",
                "label": "Evidence use",
                "description": "Checks the aggregation before changing the launch plan.",
                "weight": 5,
                "evidence": [{"source": "artifact", "ref": "transform-audit"}],
            },
            {
                "id": "risk",
                "label": "Risk control",
                "description": "Balances inventory exposure against the launch commitment.",
                "weight": 5,
                "evidence": [
                    {"source": "metric", "ref": "stockout-risk"},
                    {"source": "event", "ref": "partner-asks-for-commitment"},
                ],
            },
        ],
    }


def test_generated_spec_is_immutable_and_has_stable_semantic_fingerprint() -> None:
    first = GeneratedScenarioSpec.model_validate(scenario_data())
    cosmetic = scenario_data()
    cosmetic.update(
        {
            "scenario_id": "generated-another-identifier",
            "generation_nonce": "request-different-123",
            "title": "A completely different display title",
            "learner_role": "Planning specialist",
            "briefing": "Cosmetically rewritten briefing.",
            "first_decision": "How will you respond?",
        }
    )
    second = GeneratedScenarioSpec.model_validate(cosmetic)

    assert first.fingerprint == second.fingerprint
    assert is_near_duplicate(first, second)
    with pytest.raises(ValidationError):
        first.title = "Mutation is forbidden"  # type: ignore[misc]


def test_novelty_similarity_detects_related_challenges_without_equal_fingerprints() -> None:
    first = GeneratedScenarioSpec.model_validate(scenario_data())
    changed = scenario_data()
    changed["decision_tradeoff"] = (
        "Stopping protects inventory but delays a partner launch; proceeding may cause stockouts."
    )
    second = GeneratedScenarioSpec.model_validate(changed)

    assert first.fingerprint != second.fingerprint
    assert novelty_similarity(first, second) > 0.72
    assert is_near_duplicate(first, second)


def test_runtime_applies_generated_graph_and_due_escalations_deterministically() -> None:
    spec = GeneratedScenarioSpec.model_validate(scenario_data())
    first = GeneratedScenarioRuntime()
    second = GeneratedScenarioRuntime()
    first_id = first.create(spec)["id"]
    second_id = second.create(spec)["id"]

    for runtime, run_id in ((first, first_id), (second, second_id)):
        inspected = runtime.apply(run_id, "inspect-pipeline")
        assert inspected["sim_time"] == 5
        assert "transform-audit" in inspected["revealed_artifacts"]
        recovered = runtime.apply(run_id, "correct-and-reforecast")
        assert recovered["outcome"] == "recovered"
        assert recovered["metrics"]["stockout-risk"] == 20
        assert recovered["fired_events"] == ["partner-asks-for-commitment"]

    first_state = first.snapshot(first_id)
    second_state = second.snapshot(second_id)
    for key in (
        "sim_time",
        "metrics",
        "completed_actions",
        "revealed_artifacts",
        "fired_events",
        "events",
        "outcome",
        "scenario_fingerprint",
    ):
        assert first_state[key] == second_state[key]


def test_runtime_enforces_prerequisites_and_terminal_timed_event() -> None:
    data = scenario_data()
    actions = data["actions"]
    assert isinstance(actions, list)
    actions.append(
        {
            "id": "wait-for-more-data",
            "label": "Wait for more data",
            "description": "Delay the decision until warehouse allocation locks.",
            "risk": "high",
            "prerequisites": ["inspect-pipeline"],
            "metric_effects": [],
            "reveals_artifacts": [],
            "advances_minutes": 25,
        }
    )
    spec = GeneratedScenarioSpec.model_validate(data)
    runtime = GeneratedScenarioRuntime()
    run_id = runtime.create(spec)["id"]

    with pytest.raises(GeneratedScenarioError, match="missing prerequisites"):
        runtime.apply(run_id, "correct-and-reforecast")
    runtime.apply(run_id, "inspect-pipeline")
    terminal = runtime.apply(run_id, "wait-for-more-data")
    assert terminal["outcome"] == "terminal_escalation"
    assert terminal["fired_events"] == [
        "partner-asks-for-commitment",
        "allocation-locks",
    ]
    with pytest.raises(GeneratedScenarioError, match="already ended"):
        runtime.apply(run_id, "correct-and-reforecast")


def test_runtime_restores_a_persisted_snapshot_and_continues() -> None:
    spec = GeneratedScenarioSpec.model_validate(scenario_data())
    original = GeneratedScenarioRuntime()
    snapshot = original.create(spec)
    snapshot = original.apply(str(snapshot["id"]), "inspect-pipeline")

    restarted = GeneratedScenarioRuntime()
    assert restarted.restore(spec, snapshot) == snapshot
    recovered = restarted.apply(str(snapshot["id"]), "correct-and-reforecast")
    assert recovered["outcome"] == "recovered"

    corrupt = deepcopy(snapshot)
    corrupt["scenario_fingerprint"] = "0" * 64
    with pytest.raises(GeneratedScenarioError, match="fingerprint"):
        GeneratedScenarioRuntime().restore(spec, corrupt)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["actions"][0].update(
                {"prerequisites": ["correct-and-reforecast"]}
            ),
            "acyclic",
        ),
        (
            lambda data: data["actions"][0].update(
                {
                    "metric_effects": [
                        {"metric_id": "imaginary", "operation": "add", "value": 1}
                    ]
                }
            ),
            "unknown metric refs",
        ),
        (
            lambda data: data["success_requirements"][1].update({"threshold": 200}),
            "outside its metric range",
        ),
        (
            lambda data: [event.update({"terminal": False}) for event in data["timed_escalations"]],
            "must be terminal",
        ),
        (
            lambda data: data["rubric"][0].update(
                {"evidence": [{"source": "artifact", "ref": "missing"}]}
            ),
            "unknown artifact ref",
        ),
    ],
)
def test_generated_spec_rejects_semantically_invalid_worlds(mutate, message: str) -> None:
    data = scenario_data()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        GeneratedScenarioSpec.model_validate(data)


def test_generated_spec_rejects_a_valid_graph_with_no_recovery_path() -> None:
    data = scenario_data()
    actions = data["actions"]
    assert isinstance(actions, list)
    actions[0]["advances_minutes"] = 30

    with pytest.raises(ValidationError, match="no reachable recovery path"):
        GeneratedScenarioSpec.model_validate(data)
