"""Plain-data evidence export for the checkout worker-pool incident.

This module intentionally has no dependency on ``apprentice.incident``.  A
runtime can consume the returned mapping as scenario seed data, or an author
can inspect it over the service's ``/scenario-fixture`` HTTP endpoint.
"""

from __future__ import annotations

from copy import deepcopy

from .service import CheckoutService

SCENARIO_ID = "checkout-worker-lease-leak"
SCHEMA_VERSION = 1


def export_worker_lease_fixture(*, worker_limit: int = 4) -> dict[str, object]:
    """Create the same failure-and-recovery evidence on every invocation.

    The exported values are JSON-compatible and contain no live service state.
    They can therefore be stored with a learner run and replayed independently
    of this deliberately flawed service.
    """
    service = CheckoutService(worker_limit=worker_limit)
    for ordinal in range(1, worker_limit + 1):
        service.checkout(f"checkout-{ordinal}")
    rejected = service.checkout("checkout-backlogged")
    if rejected["accepted"]:
        raise RuntimeError("fixture must exhaust the checkout worker pool")

    failure = _evidence_snapshot(service)
    service.rollback()
    recovery = _evidence_snapshot(service)
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": SCENARIO_ID,
        "title": "Checkout worker pool exhaustion after release 2026.07.14.3",
        "failure": failure,
        "remediation": {
            "action": "rollback",
            "endpoint": "POST /admin/rollback",
            "expected_effect": "restart workers and replay queued checkout requests",
        },
        "recovery": recovery,
    }


def _evidence_snapshot(service: CheckoutService) -> dict[str, object]:
    health = service.health()
    logs = service.evidence()
    deployment = next(event for event in reversed(logs) if event["event"].startswith("deploy."))
    return {
        "health": deepcopy(health),
        "metrics": deepcopy(service.metrics()),
        "logs": deepcopy(logs),
        "deployment": deepcopy(deployment),
    }
