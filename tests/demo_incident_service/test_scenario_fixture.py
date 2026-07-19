from __future__ import annotations

from fastapi.testclient import TestClient

from demo_incident_service.app import build_app
from demo_incident_service.scenario import SCENARIO_ID, export_worker_lease_fixture


def test_fixture_exports_failure_and_rollback_evidence_deterministically() -> None:
    fixture = export_worker_lease_fixture(worker_limit=2)

    assert fixture == export_worker_lease_fixture(worker_limit=2)
    assert fixture["scenario_id"] == SCENARIO_ID
    failure = fixture["failure"]
    recovery = fixture["recovery"]
    assert failure["health"]["status"] == "degraded"
    assert failure["metrics"]["checkout_worker_leases"] == 2
    assert failure["metrics"]["checkout_backlog"] == 1
    assert failure["deployment"]["event"] == "deploy.active"
    assert failure["logs"][-1]["event"] == "checkout.rejected"
    assert recovery["health"]["status"] == "ok"
    assert recovery["metrics"]["checkout_backlog"] == 0
    assert recovery["metrics"]["checkout_recovered_total"] == 1
    assert recovery["deployment"]["event"] == "deploy.rolled_back"
    assert recovery["logs"][-1]["event"] == "deploy.rolled_back"


def test_http_fixture_export_has_the_same_runtime_integration_contract() -> None:
    client = TestClient(build_app())

    response = client.get("/scenario-fixture")

    assert response.status_code == 200
    fixture = response.json()
    assert fixture["schema_version"] == 1
    assert fixture["remediation"] == {
        "action": "rollback",
        "endpoint": "POST /admin/rollback",
        "expected_effect": "restart workers and replay queued checkout requests",
    }
    assert set(fixture["failure"]) == {"health", "metrics", "logs", "deployment"}
    assert set(fixture["recovery"]) == {"health", "metrics", "logs", "deployment"}
