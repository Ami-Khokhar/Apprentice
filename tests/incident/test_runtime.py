from __future__ import annotations

from copy import deepcopy

import pytest

import apprentice.incident.runtime as incident_runtime
from apprentice.incident import IncidentRuntime, InvalidIncidentActionError
from apprentice.ledger.db import SQLiteDatabase
from demo_incident_service.scenario import export_worker_lease_fixture


def test_same_action_sequence_has_same_canonical_outcome() -> None:
    first = IncidentRuntime()
    second = IncidentRuntime()
    first_id = first.create()["id"]
    second_id = second.create()["id"]

    for action in ("declare_incident", "investigate", "rollback", "communicate"):
        first.apply(first_id, action)
        second.apply(second_id, action)

    first_state = first.snapshot(first_id)
    second_state = second.snapshot(second_id)
    for key in (
        "sim_time",
        "severity",
        "metrics",
        "artifacts",
        "available_actions",
        "completed",
        "outcome",
        "debrief",
    ):
        assert first_state[key] == second_state[key]
    assert first.replay(first_id) == second.replay(second_id)
    assert first_state["completed"] is True
    assert first_state["outcome"] == "recovered"


def test_action_log_contains_causal_rule_and_metric_delta() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create()["id"]

    runtime.apply(incident_id, "rollback")

    rollback = runtime.replay(incident_id)[-1]
    assert rollback["rule"] == "scenario.action.rollback"
    assert rollback["delta"]["queue_depth"] == -1200
    assert rollback["time"] == 1


def test_constrained_actions_reject_invalid_precondition() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create()["id"]

    with pytest.raises(InvalidIncidentActionError, match="not available"):
        runtime.apply(incident_id, "communicate")


def test_hard_scenario_is_selectable_and_requires_evidence_before_rollback() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create(scenario_id="checkout-worker-lease-leak-hard", seed=9)["id"]

    with pytest.raises(InvalidIncidentActionError, match="not available"):
        runtime.apply(incident_id, "rollback")

    runtime.apply(incident_id, "declare_incident")
    runtime.apply(incident_id, "investigate")
    snapshot = runtime.apply(incident_id, "rollback")
    assert snapshot["scenario"] == "checkout-worker-lease-leak-hard"
    assert snapshot["seed"] == 9
    assert snapshot["metrics"]["queue_depth"] == 1440


def test_persisted_run_survives_a_runtime_restart_and_replays_snapshots(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    first = IncidentRuntime(database)
    created = first.create(seed=17)
    incident_id = created["id"]
    first.apply(incident_id, "advance_time")
    current = first.apply(incident_id, "advance_time")

    restarted = IncidentRuntime(SQLiteDatabase(tmp_path / "apprentice.db"))
    assert restarted.snapshot(incident_id) == current
    at_alert = restarted.replay_snapshot(incident_id, 0)
    assert at_alert["sim_time"] == 0
    assert at_alert["metrics"]["queue_depth"] == 1200
    assert at_alert["scenario_version"] == 1
    assert at_alert["seed"] == 17

    # Restarted runs must retain the historical snapshots needed by _save_run
    # when a learner continues the same scenario.
    continued = restarted.apply(incident_id, "declare_incident")
    assert continued["severity"] == "SEV-2"
    assert len(continued["events"]) > len(current["events"])


def test_scheduled_world_event_has_a_deterministic_causal_branch() -> None:
    undeclared = IncidentRuntime()
    declared = IncidentRuntime()
    undeclared_id = undeclared.create()["id"]
    declared_id = declared.create()["id"]

    undeclared.apply(undeclared_id, "advance_time")
    undeclared_state = undeclared.apply(undeclared_id, "advance_time")
    declared.apply(declared_id, "declare_incident")
    declared_state = declared.apply(declared_id, "advance_time")

    assert undeclared.replay(undeclared_id)[-1]["rule"] == "scenario.clock.undeclared_escalation"
    assert declared.replay(declared_id)[-1]["rule"] == "scenario.clock.command_forecast"
    assert (
        undeclared_state["metrics"]["customers_impacted"]
        > declared_state["metrics"]["customers_impacted"]
    )


def test_golden_response_has_provenance_and_evidence_linked_rubric() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create()["id"]

    for action in ("declare_incident", "investigate", "rollback", "communicate"):
        snapshot = runtime.apply(incident_id, action)

    assert snapshot["outcome"] == "recovered"
    assert {item["criterion"] for item in snapshot["debrief"]} == {
        "Timing",
        "Evidence",
        "Mitigation",
        "Communication",
        "Validation",
    }
    assert all(item["score"] == 1 and item["evidence"] for item in snapshot["debrief"])
    required_provenance = {"id", "source", "timestamp", "visibility", "version"}
    assert all(required_provenance <= artifact.keys() for artifact in snapshot["artifacts"])


def test_delay_narrows_options_then_reaches_terminal_adverse_outcome() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create()["id"]

    runtime.apply(incident_id, "advance_time")
    delayed = runtime.apply(incident_id, "advance_time")
    actions = {action["kind"]: action for action in delayed["available_actions"]}
    assert actions["mitigate"]["enabled"] is False
    assert any(event.get("actor") == "sre-oncall" for event in delayed["events"])

    runtime.apply(incident_id, "advance_time")
    terminal = runtime.apply(incident_id, "advance_time")
    assert terminal["terminal"] is True
    assert terminal["outcome"] == "terminal_escalation"
    assert all(not action["enabled"] for action in terminal["available_actions"])
    assert terminal["events"][-1]["rule"] == "scenario.clock.terminal_escalation"
    with pytest.raises(InvalidIncidentActionError, match="terminal"):
        runtime.apply(incident_id, "rollback")


def test_initial_and_recovery_artifacts_are_derived_from_worker_lease_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = deepcopy(export_worker_lease_fixture())
    fixture["title"] = "Fixture-controlled checkout failure"
    failure = fixture["failure"]
    recovery = fixture["recovery"]
    assert isinstance(failure, dict)
    assert isinstance(recovery, dict)
    deployment = failure["deployment"]
    assert isinstance(deployment, dict)
    deployment["release"] = "fixture-only-release"
    monkeypatch.setattr(incident_runtime, "export_worker_lease_fixture", lambda: fixture)

    runtime = IncidentRuntime()
    created = runtime.create()
    initial = {artifact["id"]: artifact for artifact in created["artifacts"]}
    assert created["fixture_schema_version"] == fixture["schema_version"]
    assert created["fixture_title"] == fixture["title"]
    assert created["events"][0]["detail"] == fixture["title"]
    assert initial["fixture-failure-deployment"]["evidence"] == failure["deployment"]

    recovered = runtime.apply(created["id"], "rollback")
    artifacts = {artifact["id"]: artifact for artifact in recovered["artifacts"]}
    assert artifacts["fixture-recovery-validation"]["evidence"] == recovery


def test_bounded_actor_views_only_expose_role_permitted_artifacts() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create()["id"]

    sre = runtime.actor_view(incident_id, "sre-oncall")
    support = runtime.actor_view(incident_id, "support-lead")
    comms = runtime.actor_view(incident_id, "customer-comms")

    assert {artifact["id"] for artifact in sre["visible_artifacts"]} == {
        "fixture-failure-deployment",
        "fixture-failure-rejection-log",
        "fixture-failure-metrics",
    }
    assert {artifact["id"] for artifact in support["visible_artifacts"]} == {
        "fixture-failure-metrics"
    }
    assert {artifact["id"] for artifact in comms["visible_artifacts"]} == {
        "fixture-failure-metrics"
    }
    assert comms["recommendations"] == [
        {
            "request": "draft_customer_update",
            "reason": "Await incident declaration before drafting an approved update.",
        }
    ]


def test_delegation_records_causal_actor_event_without_metric_authority(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    runtime = IncidentRuntime(database)
    incident_id = runtime.create()["id"]
    before = runtime.snapshot(incident_id)

    delegated = runtime.delegate(incident_id, "sre-oncall", "investigate_checkout")

    assert delegated["metrics"] == before["metrics"]
    event = delegated["events"][-1]
    assert event["actor"] == "sre-oncall"
    assert event["request"] == "investigate_checkout"
    assert event["rule"] == "actor.delegate.sre-oncall.investigate_checkout"
    assert event["delta"] == {}

    restarted = IncidentRuntime(SQLiteDatabase(tmp_path / "apprentice.db"))
    assert restarted.snapshot(incident_id) == delegated
    assert restarted.replay_snapshot(incident_id, int(event["event_index"])) == delegated


def test_delegation_policy_rejects_unbounded_actor_requests() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create()["id"]

    with pytest.raises(InvalidIncidentActionError, match="not permitted"):
        runtime.delegate(incident_id, "support-lead", "rollback")


def test_security_response_has_constrained_containment_and_evolving_evidence() -> None:
    runtime = IncidentRuntime()
    incident_id = runtime.create(scenario_id="credential-stuffing-response")["id"]

    with pytest.raises(InvalidIncidentActionError, match="not available"):
        runtime.apply(incident_id, "contain_sessions")

    runtime.apply(incident_id, "declare_security_incident")
    triaged = runtime.apply(incident_id, "triage_alert")
    assert triaged["events"][-1]["rule"] == "scenario.clock.security_scope_confirmed"
    contained = runtime.apply(incident_id, "contain_sessions")
    assert contained["severity"] == "Contained"
    assert "security-session-containment" in {item["id"] for item in contained["artifacts"]}
    recovered = runtime.apply(incident_id, "rotate_credentials")
    recovered = runtime.apply(incident_id, "notify_affected_users")
    assert recovered["outcome"] == "recovered"
    assert recovered["metrics"]["active_sessions"] == 0
    assert all(item["score"] == 1 for item in recovered["debrief"])


def test_model_quality_scenario_has_distinct_grounded_recovery_path() -> None:
    runtime = IncidentRuntime()
    created = runtime.create(scenario_id="model-quality-regression")
    incident_id = created["id"]

    assert created["fixture_title"] == "Production model quality regression"
    assert created["metrics"]["evaluation_pass_rate"] == 61
    assert "inspect_evaluations" in {
        action["kind"] for action in created["available_actions"]
    }
    with pytest.raises(InvalidIncidentActionError, match="not available"):
        runtime.apply(incident_id, "rollback_model_release")

    runtime.apply(incident_id, "declare_model_incident")
    investigated = runtime.apply(incident_id, "inspect_evaluations")
    assert "model-regression-finding" in {
        artifact["id"] for artifact in investigated["artifacts"]
    }
    recovered = runtime.apply(incident_id, "rollback_model_release")
    recovered = runtime.apply(incident_id, "notify_product_owners")

    assert recovered["outcome"] == "recovered"
    assert recovered["metrics"]["evaluation_pass_rate"] == 98
    assert "model-recovery-evaluation" in {
        artifact["id"] for artifact in recovered["artifacts"]
    }
