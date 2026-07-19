from __future__ import annotations

from fastapi.testclient import TestClient

from demo_incident_service.app import build_app
from demo_incident_service.service import CheckoutService


def test_worker_lease_regression_exhausts_pool_with_observable_evidence() -> None:
    service = CheckoutService(worker_limit=2)

    assert service.checkout("order-1")["accepted"] is True
    assert service.checkout("order-2")["accepted"] is True
    rejected = service.checkout("order-3")

    assert rejected == {
        "accepted": False,
        "order_id": "order-3",
        "status_code": 503,
        "reason": "checkout workers exhausted; order queued for retry",
    }
    assert service.health()["status"] == "degraded"
    assert service.metrics()["checkout_backlog"] == 1
    assert service.metrics()["checkout_worker_leases"] == 2
    assert service.evidence()[-1]["event"] == "checkout.rejected"
    assert service.evidence()[-1]["reason"] == "worker_pool_exhausted"


def test_rollback_restarts_workers_replays_backlog_and_recovers_service() -> None:
    service = CheckoutService(worker_limit=2)
    service.checkout("order-1")
    service.checkout("order-2")
    service.checkout("order-3")

    snapshot = service.rollback()

    assert snapshot["health"]["status"] == "ok"
    assert snapshot["health"]["release"] == "checkout-api-2026.07.12.8"
    assert snapshot["metrics"]["checkout_backlog"] == 0
    assert snapshot["metrics"]["checkout_recovered_total"] == 1
    assert service.checkout("order-4")["accepted"] is True
    assert service.checkout("order-5")["worker_lease"] == "released"
    assert service.health()["worker_pool"]["leased"] == 0
    assert service.evidence()[-1]["event"] == "checkout.completed"


def test_http_harness_exposes_failure_and_safe_remediation() -> None:
    client = TestClient(build_app(CheckoutService(worker_limit=2)))

    assert client.post("/checkout", json={"order_id": "one"}).status_code == 200
    assert client.post("/checkout", json={"order_id": "two"}).status_code == 200
    failed = client.post("/checkout", json={"order_id": "three"})
    assert failed.status_code == 503
    assert client.get("/health").json()["status"] == "degraded"
    assert client.get("/metrics").json()["checkout_backlog"] == 1
    assert client.get("/evidence").json()[-1]["event"] == "checkout.rejected"

    repaired = client.post("/admin/rollback")
    assert repaired.status_code == 200
    assert repaired.json()["health"]["status"] == "ok"
    assert repaired.json()["metrics"]["checkout_backlog"] == 0
    assert client.post("/checkout", json={"order_id": "four"}).status_code == 200
