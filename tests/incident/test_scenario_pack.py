from __future__ import annotations

from dataclasses import replace

from apprentice.incident.scenario_pack import (
    CHECKOUT_WORKER_LEASE_HARD_PACK,
    CHECKOUT_WORKER_LEASE_PACK,
    CUSTOMER_ESCALATION_PACK,
    SECURITY_RESPONSE_PACK,
    lint_scenario_pack,
    lint_scenario_registry,
)
from apprentice.incident.verification import (
    verify_checkout_paths,
    verify_customer_escalation_paths,
    verify_hard_checkout_paths,
    verify_security_response_paths,
)


def test_checkout_pack_is_linted_and_has_one_versioned_registry_entry() -> None:
    assert lint_scenario_registry() == []
    assert CHECKOUT_WORKER_LEASE_PACK.id == "checkout-worker-lease-leak"
    assert CHECKOUT_WORKER_LEASE_PACK.version == 1


def test_lint_reports_missing_authoring_metadata() -> None:
    malformed = replace(
        CHECKOUT_WORKER_LEASE_PACK,
        actions={"declare_incident": {"label": "Declare"}},
    )
    errors = lint_scenario_pack(malformed)
    assert "action declare_incident is missing learner-facing metadata" in errors
    assert "action declare_incident is missing its state field" in errors


def test_checkout_pack_deterministically_verifies_golden_and_adverse_paths() -> None:
    assert verify_checkout_paths() == {
        "scenario_id": "checkout-worker-lease-leak",
        "version": 1,
        "golden_outcome": "recovered",
        "adverse_outcome": "terminal_escalation",
    }


def test_hard_pack_has_constrained_rollback_and_deterministic_paths() -> None:
    assert CHECKOUT_WORKER_LEASE_HARD_PACK.actions["rollback"]["requires"] == ("investigated",)
    assert verify_hard_checkout_paths() == {
        "scenario_id": "checkout-worker-lease-leak-hard",
        "version": 1,
        "golden_outcome": "recovered",
        "adverse_outcome": "terminal_escalation",
    }


def test_customer_escalation_pack_is_runnable_not_just_registered() -> None:
    assert CUSTOMER_ESCALATION_PACK.domain == "customer_escalation"
    assert verify_customer_escalation_paths() == {
        "scenario_id": "enterprise-renewal-escalation",
        "version": 1,
        "golden_outcome": "recovered",
        "adverse_outcome": "terminal_escalation",
    }


def test_security_response_pack_is_runnable_with_recovery_and_terminal_paths() -> None:
    assert SECURITY_RESPONSE_PACK.domain == "security_response"
    assert verify_security_response_paths() == {
        "scenario_id": "credential-stuffing-response",
        "version": 1,
        "golden_outcome": "recovered",
        "adverse_outcome": "terminal_escalation",
    }
