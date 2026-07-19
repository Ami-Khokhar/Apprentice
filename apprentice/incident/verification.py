"""Fast deterministic acceptance checks for the authored checkout scenario."""

from __future__ import annotations

from .runtime import IncidentRuntime
from .scenario_pack import (
    CHECKOUT_WORKER_LEASE_HARD_PACK,
    CHECKOUT_WORKER_LEASE_PACK,
    CUSTOMER_ESCALATION_PACK,
    SECURITY_RESPONSE_PACK,
    lint_scenario_registry,
)


def verify_security_response_paths() -> dict[str, object]:
    """Exercise containment and recovery in the security-response domain pack."""
    if errors := lint_scenario_registry():
        raise ValueError(f"Scenario pack lint failed: {', '.join(errors)}")
    golden = IncidentRuntime()
    golden_id = str(golden.create(scenario_id=SECURITY_RESPONSE_PACK.id)["id"])
    for action in (
        "declare_security_incident",
        "triage_alert",
        "contain_sessions",
        "rotate_credentials",
        "notify_affected_users",
    ):
        snapshot = golden.apply(golden_id, action)
    if snapshot["outcome"] != "recovered" or any(
        item["score"] != 1 for item in snapshot["debrief"]
    ):
        raise AssertionError("security response golden path must contain and recover accounts")
    adverse = IncidentRuntime()
    adverse_id = str(adverse.create(scenario_id=SECURITY_RESPONSE_PACK.id)["id"])
    for _ in range(4):
        adverse_snapshot = adverse.apply(adverse_id, "advance_time")
    if adverse_snapshot["outcome"] != "terminal_escalation":
        raise AssertionError("uncontained security response must reach the terminal outcome")
    return {
        "scenario_id": SECURITY_RESPONSE_PACK.id,
        "version": SECURITY_RESPONSE_PACK.version,
        "golden_outcome": snapshot["outcome"],
        "adverse_outcome": adverse_snapshot["outcome"],
    }


def verify_customer_escalation_paths() -> dict[str, object]:
    """Exercise a second domain pack through the same deterministic runtime."""
    if errors := lint_scenario_registry():
        raise ValueError(f"Scenario pack lint failed: {', '.join(errors)}")
    golden = IncidentRuntime()
    golden_id = str(golden.create(scenario_id=CUSTOMER_ESCALATION_PACK.id)["id"])
    for action in ("acknowledge_escalation", "review_case", "approve_remedy", "send_resolution"):
        snapshot = golden.apply(golden_id, action)
    if snapshot["outcome"] != "recovered" or any(
        item["score"] != 1 for item in snapshot["debrief"]
    ):
        raise AssertionError("customer escalation golden path must resolve the account")
    adverse = IncidentRuntime()
    adverse_id = str(adverse.create(scenario_id=CUSTOMER_ESCALATION_PACK.id)["id"])
    for _ in range(4):
        adverse_snapshot = adverse.apply(adverse_id, "advance_time")
    if adverse_snapshot["outcome"] != "terminal_escalation":
        raise AssertionError("unowned customer escalation must reach the renewal-risk outcome")
    return {
        "scenario_id": CUSTOMER_ESCALATION_PACK.id,
        "version": CUSTOMER_ESCALATION_PACK.version,
        "golden_outcome": snapshot["outcome"],
        "adverse_outcome": adverse_snapshot["outcome"],
    }


def verify_checkout_paths() -> dict[str, object]:
    """Exercise the intended and delayed paths without HTTP or wall-clock inputs.

    This is deliberately a compact authoring check: it proves pack metadata and
    engine mechanics still produce the two outcomes the scenario promises.
    """
    lint_errors = lint_scenario_registry()
    if lint_errors:
        raise ValueError(f"Scenario pack lint failed: {', '.join(lint_errors)}")

    golden = IncidentRuntime()
    golden_id = str(golden.create(scenario_id=CHECKOUT_WORKER_LEASE_PACK.id)["id"])
    for action in ("declare_incident", "investigate", "rollback", "communicate"):
        golden_snapshot = golden.apply(golden_id, action)
    if golden_snapshot["outcome"] != "recovered":
        raise AssertionError("golden path must recover checkout")
    if any(item["score"] != 1 for item in golden_snapshot["debrief"]):
        raise AssertionError("golden path must satisfy every rubric criterion")

    adverse = IncidentRuntime()
    adverse_id = str(adverse.create(scenario_id=CHECKOUT_WORKER_LEASE_PACK.id)["id"])
    for _ in range(4):
        adverse_snapshot = adverse.apply(adverse_id, "advance_time")
    if adverse_snapshot["outcome"] != "terminal_escalation":
        raise AssertionError("delayed path must reach terminal escalation")
    if adverse_snapshot["events"][-1]["rule"] != "scenario.clock.terminal_escalation":
        raise AssertionError("adverse outcome must retain its causal rule")

    return {
        "scenario_id": CHECKOUT_WORKER_LEASE_PACK.id,
        "version": CHECKOUT_WORKER_LEASE_PACK.version,
        "golden_outcome": golden_snapshot["outcome"],
        "adverse_outcome": adverse_snapshot["outcome"],
    }


def verify_hard_checkout_paths() -> dict[str, object]:
    """Verify the constrained variant remains deterministic and meaningfully harder."""
    lint_errors = lint_scenario_registry()
    if lint_errors:
        raise ValueError(f"Scenario pack lint failed: {', '.join(lint_errors)}")

    golden = IncidentRuntime()
    golden_id = str(golden.create(scenario_id=CHECKOUT_WORKER_LEASE_HARD_PACK.id)["id"])
    for action in ("declare_incident", "investigate", "rollback", "communicate", "advance_time"):
        golden_snapshot = golden.apply(golden_id, action)
    if golden_snapshot["outcome"] != "recovered":
        raise AssertionError("hard golden path must recover checkout")

    adverse = IncidentRuntime()
    adverse_id = str(adverse.create(scenario_id=CHECKOUT_WORKER_LEASE_HARD_PACK.id)["id"])
    for _ in range(4):
        adverse_snapshot = adverse.apply(adverse_id, "advance_time")
    if adverse_snapshot["outcome"] != "terminal_escalation":
        raise AssertionError("hard delayed path must reach terminal escalation")

    return {
        "scenario_id": CHECKOUT_WORKER_LEASE_HARD_PACK.id,
        "version": CHECKOUT_WORKER_LEASE_HARD_PACK.version,
        "golden_outcome": golden_snapshot["outcome"],
        "adverse_outcome": adverse_snapshot["outcome"],
    }
