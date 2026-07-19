from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

from apprentice.canonical import digest
from apprentice.ledger.repository import Repository
from apprentice.sidecar.app import build_sidecar


def activate_bucket(repository: Repository, **create_kwargs):
    bucket = repository.create_bucket(name="file-expense", **create_kwargs)
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    from apprentice.sidecar.run_service import RunService

    return RunService(repository).activate_capability(
        bucket.id, reviewed_playbook={"task": "file expense", "version": 1}
    )


def test_http_creates_run_with_server_owned_digest_and_version(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    activate_bucket(
        repository,
        target_base_url="http://127.0.0.1:8000",
    )
    client = TestClient(build_sidecar(repository=repository))

    response = client.post(
        "/api/runs",
        json={
            "bucket_name": "file-expense",
            "inputs": {"amount": "42.00", "merchant": "Train Cafe"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "created"
    assert body["playbook_version"] == 1
    assert body["input_digest"] == digest({"amount": "42.00", "merchant": "Train Cafe"})
    assert body["allowed_hosts"] == ["127.0.0.1"]
    assert body["target_base_url"] == "http://127.0.0.1:8000"


def test_http_caller_cannot_supply_level_weight_or_rehearsal_claim(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    activate_bucket(repository)
    client = TestClient(build_sidecar(repository=repository))

    forged_bucket = client.post(
        "/api/buckets",
        json={
            "name": "forged",
            "kind": "playbook",
            "risk_class": "low",
            "origin": "demonstrated",
            "playbook_version": 1,
            "level": 4,
        },
    )
    forged_run = client.post(
        "/api/runs",
        json={
            "bucket_name": "file-expense",
            "inputs": {"amount": "42.00"},
            "target_base_url": "http://127.0.0.1:8000",
            "rehearsed": True,
            "weight": 100,
            "level": 4,
            "input_digest": "caller-controlled",
        },
    )

    assert forged_bucket.status_code == 405
    assert forged_run.status_code == 422
    assert repository.get_bucket_by_name("file-expense").level == 1
    assert repository.count_events(repository.get_bucket_by_name("file-expense").id) == 0


def test_unknown_bucket_returns_typed_not_found(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))

    response = client.post(
        "/api/runs",
        json={
            "bucket_name": "missing",
            "inputs": {},
        },
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "unknown_bucket", "message": "Unknown bucket: missing"}
    }


def test_run_caller_cannot_override_capability_target(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    activate_bucket(
        repository,
        target_base_url="http://approved.example/expense",
    )
    client = TestClient(build_sidecar(repository=repository))

    response = client.post(
        "/api/runs",
        json={
            "bucket_name": "file-expense",
            "inputs": {"amount": 42},
            "target_base_url": "http://attacker.example",
        },
    )

    assert response.status_code == 422
    assert repository.list_runs() == []


def test_http_has_no_capability_activation_shortcut(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))
    request = {
        "name": "file-expense",
        "kind": "playbook",
        "risk_class": "medium",
        "origin": "demonstrated",
        "playbook_version": 1,
    }

    assert client.post("/api/buckets", json=request).status_code == 405


def test_policy_expense_lesson_teaches_then_tests_a_real_read_only_tool(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    client = TestClient(build_sidecar(repository=repository))

    before = client.post("/api/lessons/policy-expense/test")
    taught = client.post("/api/lessons/policy-expense/teach")
    tested = client.post("/api/lessons/policy-expense/test")

    assert before.status_code == 409
    assert taught.status_code == 200
    assert taught.json()["taught"] is True
    assert taught.json()["tool_level"] == 1
    assert tested.status_code == 200
    assert tested.json()["tool_level"] == 2
    assert tested.json()["policy"]["gl_code"] == "7200"


def test_policy_expense_analysis_responds_to_a_user_demonstration(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))

    response = client.post(
        "/api/lessons/policy-expense/analyze",
        json={
            "task": "File a policy-compliant expense",
            "observed_fields": ["merchant", "amount", "receipt"],
        },
    )

    assert response.status_code == 200
    assert response.json()["supported"] is True
    assert response.json()["missing_capability"] == "lookup_expense_policy"


def test_incident_api_exposes_deterministic_cockpit_replay_and_debrief(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))

    created = client.post("/api/incidents")
    assert created.status_code == 201
    incident = created.json()
    incident_id = incident["id"]
    assert incident["severity"] == "Undeclared"
    assert {action["kind"] for action in incident["available_actions"]} == {
        "declare_incident",
        "investigate",
        "rollback",
        "mitigate",
        "communicate",
        "advance_time",
    }

    actions_url = f"/api/incidents/{incident_id}/actions"
    assert client.post(actions_url, json={"kind": "declare_incident"}).status_code == 200
    assert client.post(actions_url, json={"kind": "rollback"}).status_code == 200
    recovered = client.post(f"/api/incidents/{incident_id}/actions", json={"kind": "communicate"})
    assert recovered.status_code == 200
    assert recovered.json()["completed"] is True

    replay = client.get(f"/api/incidents/{incident_id}/replay")
    debrief = client.get(f"/api/incidents/{incident_id}/debrief")
    assert replay.status_code == 200
    assert replay.json()["events"][-1]["rule"] == "scenario.action.communicate"
    assert debrief.json()["outcome"] == "recovered"


def test_incident_api_rejects_an_unavailable_action(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))
    incident_id = client.post("/api/incidents").json()["id"]

    response = client.post(f"/api/incidents/{incident_id}/actions", json={"kind": "communicate"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_incident_action"


def test_incident_actor_api_exposes_bounded_views_and_persists_delegation(tmp_path) -> None:
    db_path = tmp_path / "apprentice.db"
    client = TestClient(build_sidecar(db_path=db_path))
    incident_id = client.post("/api/incidents").json()["id"]

    actor = client.get(f"/api/incidents/{incident_id}/actors/customer-comms")
    delegated = client.post(
        f"/api/incidents/{incident_id}/delegations",
        json={"actor_id": "customer-comms", "request": "draft_customer_update"},
    )

    assert actor.status_code == 200
    assert actor.json()["allowed_requests"] == ["draft_customer_update"]
    assert {item["id"] for item in actor.json()["visible_artifacts"]} == {"fixture-failure-metrics"}
    assert delegated.status_code == 200
    assert delegated.json()["events"][-1]["rule"] == (
        "actor.delegate.customer-comms.draft_customer_update"
    )
    assert delegated.json()["events"][-1]["delta"] == {}

    restarted = TestClient(build_sidecar(db_path=db_path))
    replay = restarted.get(f"/api/incidents/{incident_id}/replay?at=1").json()
    assert replay["snapshot"]["events"][-1]["actor"] == "customer-comms"


def test_incident_api_selects_the_hard_checkout_variant(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))
    created = client.post(
        "/api/incidents",
        json={"scenario_id": "checkout-worker-lease-leak-hard", "seed": 23},
    )

    assert created.status_code == 201
    incident = created.json()
    assert incident["scenario"] == "checkout-worker-lease-leak-hard"
    assert incident["seed"] == 23
    assert incident["metrics"]["queue_depth"] == 1800
    blocked = client.post(f"/api/incidents/{incident['id']}/actions", json={"kind": "rollback"})
    assert blocked.status_code == 409


def test_incident_api_runs_customer_escalation_through_state_replay_and_debrief(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))
    created = client.post("/api/incidents", json={"scenario_id": "enterprise-renewal-escalation"})
    assert created.status_code == 201
    incident = created.json()
    assert incident["metric_labels"]["minutes_to_breach"] == "Minutes to response breach"
    assert {action["kind"] for action in incident["available_actions"]} == {
        "acknowledge_escalation",
        "review_case",
        "approve_remedy",
        "send_resolution",
        "advance_time",
    }
    for action in ("acknowledge_escalation", "review_case", "approve_remedy", "send_resolution"):
        response = client.post(f"/api/incidents/{incident['id']}/actions", json={"kind": action})
        assert response.status_code == 200
    assert response.json()["outcome"] == "recovered"
    assert all(item["score"] == 1 for item in response.json()["debrief"])
    replay = client.get(f"/api/incidents/{incident['id']}/replay?at=4").json()["snapshot"]
    assert replay["events"][-1]["rule"] == "scenario.action.approve_remedy"


def test_incident_api_persists_security_response_replay_and_debrief(tmp_path) -> None:
    db_path = tmp_path / "apprentice.db"
    first = TestClient(build_sidecar(db_path=db_path))
    created = first.post("/api/incidents", json={"scenario_id": "credential-stuffing-response"})
    assert created.status_code == 201
    incident = created.json()
    assert incident["metric_labels"]["active_sessions"] == "Active suspect sessions"
    for action in (
        "declare_security_incident",
        "triage_alert",
        "contain_sessions",
        "rotate_credentials",
        "notify_affected_users",
    ):
        response = first.post(f"/api/incidents/{incident['id']}/actions", json={"kind": action})
        assert response.status_code == 200
    current = response.json()
    assert current["outcome"] == "recovered"
    assert all(item["score"] == 1 for item in current["debrief"])

    restarted = TestClient(build_sidecar(db_path=db_path))
    assert restarted.get(f"/api/incidents/{incident['id']}").json() == current
    historical = restarted.get(f"/api/incidents/{incident['id']}/replay?at=2").json()
    assert historical["snapshot"]["events"][-1]["rule"] == "scenario.action.triage_alert"


def test_incident_api_persists_and_returns_historical_state_after_restart(tmp_path) -> None:
    db_path = tmp_path / "apprentice.db"
    first = TestClient(build_sidecar(db_path=db_path))
    created = first.post("/api/incidents", json={"seed": 11}).json()
    incident_id = created["id"]
    first.post(f"/api/incidents/{incident_id}/actions", json={"kind": "advance_time"})
    current = first.post(
        f"/api/incidents/{incident_id}/actions", json={"kind": "advance_time"}
    ).json()

    restarted = TestClient(build_sidecar(db_path=db_path))
    assert restarted.get(f"/api/incidents/{incident_id}").json() == current
    historical = restarted.get(f"/api/incidents/{incident_id}/replay?at=0")
    assert historical.status_code == 200
    assert historical.json()["event_index"] == 0
    assert historical.json()["snapshot"]["sim_time"] == 0
    assert historical.json()["snapshot"]["seed"] == 11


def test_capability_page_exposes_operator_detail(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = activate_bucket(repository)
    client = TestClient(build_sidecar(repository=repository))

    response = client.get(f"/console/capabilities/{bucket.id}")

    assert response.status_code == 200
    assert "Promotion status" in response.text
    assert "Runs for this capability" in response.text


def test_sidecar_has_no_public_evidence_or_generic_transition_endpoint(tmp_path) -> None:
    client = TestClient(build_sidecar(db_path=tmp_path / "apprentice.db"))

    assert client.post("/api/events", json={"type": "run_success"}).status_code == 404
    assert (
        client.post(
            "/api/runs/run-1/transition",
            json={"new_state": "rehearsed"},
        ).status_code
        == 404
    )


def test_concurrent_http_run_creation_keeps_server_owned_snapshots(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    activate_bucket(
        repository,
        target_base_url="http://127.0.0.1:8000",
    )
    app = build_sidecar(repository=repository)
    barrier = Barrier(2)

    def create(amount: int):
        with TestClient(app) as client:
            barrier.wait()
            return client.post(
                "/api/runs",
                json={"bucket_name": "file-expense", "inputs": {"amount": amount}},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create, [41, 42]))

    assert [response.status_code for response in responses] == [201, 201]
    bodies = [response.json() for response in responses]
    assert len({body["id"] for body in bodies}) == 2
    assert {tuple(body["allowed_hosts"]) for body in bodies} == {("127.0.0.1",)}
    assert len(repository.list_runs()) == 2
