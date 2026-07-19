"""Versioned, reviewable authored content for Incident Command scenarios.

The engine in :mod:`apprentice.incident.runtime` owns causal state changes. This
module owns the scenario contract a content author can inspect: learner actions,
clock events, evidence metadata, role fit, and the debrief rubric. Each pack opts
into an explicitly supported world family rather than a model-authored scenario DSL.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from demo_incident_service.scenario import SCENARIO_ID, SCHEMA_VERSION


@dataclass(frozen=True)
class ScenarioPack:
    id: str
    version: int
    title: str
    initial_metrics: dict[str, float]
    actions: dict[str, dict[str, Any]]
    scheduled_events: dict[int, tuple[dict[str, Any], ...]]
    artifacts: dict[str, dict[str, str]]
    rubric: tuple[dict[str, str], ...]
    # A pack is authored content plus the small deterministic world rule set it
    # opts into.  New domains must name a supported world explicitly; this is a
    # contract, not a registry entry that the runtime cannot execute.
    domain: str = "incident_command"
    metric_labels: dict[str, str] | None = None
    role_signals: tuple[str, ...] = ()


CHECKOUT_WORKER_LEASE_PACK = ScenarioPack(
    id=SCENARIO_ID,
    version=SCHEMA_VERSION,
    title="Checkout worker pool exhaustion",
    initial_metrics={
        "p95": 2450.0,
        "error_rate": 8.2,
        "payment_success": 91.8,
        "queue_depth": 1200.0,
        "customers_impacted": 40.0,
    },
    actions={
        "declare_incident": {
            "label": "Declare SEV-2",
            "description": "Create the incident channel and assign command.",
            "risk": "low",
            "field": "declared",
            "event_type": "command",
            "detail": "SEV-2 declared; incident command is active.",
        },
        "investigate": {
            "label": "Inspect evidence",
            "description": "Review deployment, metrics, and checkout logs.",
            "risk": "low",
            "field": "investigated",
            "event_type": "evidence",
            "detail": "Deployment and logs point to a checkout worker regression.",
        },
        "rollback": {
            "label": "Roll back deploy",
            "description": "Restore the known-good checkout release.",
            "risk": "medium",
            "field": "rollback",
            "event_type": "mitigation",
            "detail_from_fixture": "remediation.expected_effect",
        },
        "mitigate": {
            "label": "Raise worker capacity",
            "description": "Reduce queue pressure while diagnosis continues.",
            "risk": "medium",
            "field": "mitigated",
            "event_type": "mitigation",
            "detail": "Worker capacity raised; the queue grows more slowly.",
        },
        "communicate": {
            "label": "Send customer update",
            "description": "Publish a stakeholder update after declaring.",
            "risk": "low",
            "field": "communicated",
            "event_type": "communication",
            "detail": "Customer-facing status update published.",
        },
        "advance_time": {
            "label": "Wait one minute",
            "description": "Let the incident evolve before taking another action.",
            "risk": "medium",
            "event_type": "clock",
            "detail": "One simulated minute elapsed while the incident remained active.",
        },
    },
    scheduled_events={
        2: (
            {
                "id": "command_forecast",
                "type": "escalation",
                "title": "Incident clock: T+2",
                "actor": "sre-oncall",
                "rule": "scenario.clock.command_forecast",
                "detail": "Incident command receives the queue-growth forecast.",
            },
            {
                "id": "undeclared_escalation",
                "type": "escalation",
                "title": "Incident clock: T+2",
                "actor": "sre-oncall",
                "rule": "scenario.clock.undeclared_escalation",
                "detail": "On-call escalation: no incident command is assigned as impact expands.",
            },
        ),
        3: (
            {
                "id": "support_ticket_surge",
                "type": "ticket",
                "title": "Support tickets surge",
                "actor": "support-lead",
                "rule": "scenario.clock.support_ticket_surge",
                "detail": (
                    "Payment support reports a sustained increase in failed-checkout contacts."
                ),
            },
            {
                "id": "comms_status_reused",
                "type": "actor",
                "title": "Customer communications check-in",
                "actor": "customer-comms",
                "rule": "scenario.clock.comms_status_reused",
                "detail": (
                    "Customer comms reuses the published status update for support responses."
                ),
            },
            {
                "id": "comms_unprepared",
                "type": "actor",
                "title": "Customer communications check-in",
                "actor": "customer-comms",
                "rule": "scenario.clock.comms_unprepared",
                "detail": (
                    "Customer comms has no approved status update to share with affected customers."
                ),
            },
        ),
        4: (
            {
                "id": "terminal_escalation",
                "type": "outcome",
                "title": "Terminal escalation",
                "actor": "incident-system",
                "rule": "scenario.clock.terminal_escalation",
                "detail": (
                    "The unmitigated checkout incident crossed the escalation window; "
                    "this training run is now terminal."
                ),
            },
        ),
    },
    artifacts={
        "failure_deployment": {
            "id": "fixture-failure-deployment",
            "title": "Deployment record",
            "kind": "deploy",
            "status": "new",
            "source": "worker-lease-fixture.failure.deployment",
        },
        "failure_rejection_log": {
            "id": "fixture-failure-rejection-log",
            "title": "Checkout error log",
            "kind": "logs",
            "status": "critical",
            "source": "worker-lease-fixture.failure.logs",
        },
        "failure_metrics": {
            "id": "fixture-failure-metrics",
            "title": "Queue dashboard",
            "kind": "metrics",
            "status": "critical",
            "source": "worker-lease-fixture.failure.metrics",
        },
        "investigation": {
            "id": "finding-worker-concurrency",
            "title": "Investigation finding",
            "kind": "evidence",
            "status": "confirmed",
            "source": "sre-oncall",
        },
        "support_surge": {
            "id": "support-checkout-contact-surge",
            "title": "Support contact report",
            "kind": "ticket",
            "status": "critical",
            "source": "support-lead",
        },
        "recovery": {
            "id": "fixture-recovery-validation",
            "title": "Rollback validation",
            "kind": "validation",
            "status": "confirmed",
            "source": "worker-lease-fixture.recovery",
        },
    },
    rubric=(
        {"criterion": "Timing", "evidence_artifact": "scenario.action.declare_incident"},
        {"criterion": "Evidence", "evidence_artifact": "finding-worker-concurrency"},
        {"criterion": "Mitigation", "evidence_artifact": "fixture-failure-deployment"},
        {"criterion": "Communication", "evidence_artifact": "customer-comms"},
        {"criterion": "Validation", "evidence_artifact": "fixture-recovery-validation"},
    ),
    role_signals=("site reliability", "sre", "devops", "platform", "operations"),
)

CHECKOUT_WORKER_LEASE_HARD_PACK = ScenarioPack(
    id="checkout-worker-lease-leak-hard",
    version=1,
    title="Checkout worker pool exhaustion: constrained evidence",
    initial_metrics={
        "p95": 3100.0,
        "error_rate": 10.4,
        "payment_success": 89.6,
        "queue_depth": 1800.0,
        "customers_impacted": 75.0,
    },
    actions={
        **deepcopy(CHECKOUT_WORKER_LEASE_PACK.actions),
        "rollback": {
            **deepcopy(CHECKOUT_WORKER_LEASE_PACK.actions["rollback"]),
            "requires": ("investigated",),
            "description": "Restore the known-good release after confirming the evidence.",
        },
        "mitigate": {
            **deepcopy(CHECKOUT_WORKER_LEASE_PACK.actions["mitigate"]),
            "latest_time": 1,
            "description": "Raise capacity before the first escalation window closes.",
        },
    },
    scheduled_events=deepcopy(CHECKOUT_WORKER_LEASE_PACK.scheduled_events),
    artifacts=deepcopy(CHECKOUT_WORKER_LEASE_PACK.artifacts),
    rubric=CHECKOUT_WORKER_LEASE_PACK.rubric,
    role_signals=CHECKOUT_WORKER_LEASE_PACK.role_signals,
)


CUSTOMER_ESCALATION_PACK = ScenarioPack(
    id="enterprise-renewal-escalation",
    version=1,
    title="Enterprise renewal escalation",
    initial_metrics={
        "minutes_to_breach": 90.0,
        "customer_sentiment": 32.0,
        "resolution_confidence": 18.0,
        "open_escalations": 1.0,
        "accounts_at_risk": 1.0,
    },
    actions={
        "acknowledge_escalation": {
            "label": "Acknowledge executive escalation",
            "description": "Take ownership and establish the response channel.",
            "risk": "low",
            "field": "declared",
            "event_type": "command",
            "detail": (
                "Account owner acknowledged the executive escalation and set a response cadence."
            ),
        },
        "review_case": {
            "label": "Review account evidence",
            "description": "Inspect the CRM thread, entitlement, and call transcript.",
            "risk": "low",
            "field": "investigated",
            "event_type": "evidence",
            "requires": ("declared",),
            "detail": (
                "The account is blocked by a provisioning gap covered by the enterprise "
                "entitlement."
            ),
        },
        "approve_remedy": {
            "label": "Approve service remedy",
            "description": "Authorize the documented provisioning fix and credit.",
            "risk": "medium",
            "field": "rollback",
            "event_type": "remediation",
            "requires": ("investigated",),
            "detail": (
                "The provisioning fix and service credit were approved against the entitlement."
            ),
        },
        "send_resolution": {
            "label": "Send resolution update",
            "description": "Share the confirmed remedy and next checkpoint with the customer.",
            "risk": "low",
            "field": "communicated",
            "event_type": "communication",
            "requires": ("declared", "rollback"),
            "detail": "Customer received the confirmed remedy, credit, and follow-up checkpoint.",
        },
        "advance_time": {
            "label": "Wait fifteen minutes",
            "description": "Let the escalation clock advance before taking another action.",
            "risk": "medium",
            "event_type": "clock",
            "detail": "Fifteen simulated minutes elapsed while the escalation remained open.",
        },
    },
    scheduled_events={
        2: (
            {
                "id": "exec-followup",
                "type": "escalation",
                "title": "Executive follow-up due",
                "actor": "customer-executive",
                "rule": "scenario.clock.executive_followup",
                "detail": "The customer executive asks for a named owner and a confirmed remedy.",
            },
        ),
        4: (
            {
                "id": "renewal-risk",
                "type": "outcome",
                "title": "Renewal decision escalated",
                "actor": "customer-executive",
                "rule": "scenario.clock.renewal_risk",
                "detail": (
                    "No confirmed remedy arrived before the renewal review; this training run "
                    "is terminal."
                ),
            },
        ),
    },
    artifacts={
        "crm_thread": {
            "id": "customer-crm-thread",
            "title": "CRM escalation thread",
            "kind": "case",
            "status": "critical",
            "source": "crm",
        },
        "entitlement": {
            "id": "customer-entitlement",
            "title": "Enterprise entitlement",
            "kind": "contract",
            "status": "confirmed",
            "source": "contract-system",
        },
        "call": {
            "id": "customer-call-transcript",
            "title": "Executive call transcript",
            "kind": "transcript",
            "status": "critical",
            "source": "call-recording",
        },
        "finding": {
            "id": "finding-provisioning-gap",
            "title": "Account investigation finding",
            "kind": "evidence",
            "status": "confirmed",
            "source": "account-owner",
        },
        "remedy": {
            "id": "approved-service-remedy",
            "title": "Approved service remedy",
            "kind": "approval",
            "status": "confirmed",
            "source": "support-operations",
        },
    },
    rubric=(
        {"criterion": "Ownership", "evidence_artifact": "scenario.action.acknowledge_escalation"},
        {"criterion": "Evidence", "evidence_artifact": "finding-provisioning-gap"},
        {"criterion": "Remedy", "evidence_artifact": "approved-service-remedy"},
        {"criterion": "Communication", "evidence_artifact": "scenario.action.send_resolution"},
    ),
    domain="customer_escalation",
    metric_labels={
        "minutes_to_breach": "Minutes to response breach",
        "customer_sentiment": "Customer sentiment",
        "resolution_confidence": "Resolution confidence",
        "open_escalations": "Open escalations",
        "accounts_at_risk": "Accounts at risk",
    },
    role_signals=("customer success", "support", "account management", "sales"),
)


SECURITY_RESPONSE_PACK = ScenarioPack(
    id="credential-stuffing-response",
    version=1,
    title="Credential-stuffing response",
    initial_metrics={
        "active_sessions": 36.0,
        "suspicious_events": 148.0,
        "data_exposure_risk": 58.0,
        "containment_confidence": 12.0,
        "accounts_at_risk": 36.0,
    },
    actions={
        "declare_security_incident": {
            "label": "Declare security incident",
            "description": "Assign an incident lead and open the security response channel.",
            "risk": "low",
            "field": "declared",
            "event_type": "command",
            "detail": "Security incident declared; incident lead and response channel assigned.",
        },
        "triage_alert": {
            "label": "Triage identity evidence",
            "description": "Review identity-provider, session, and audit evidence.",
            "risk": "low",
            "field": "investigated",
            "event_type": "evidence",
            "requires": ("declared",),
            "detail": (
                "Identity evidence confirms automated credential stuffing against reused accounts."
            ),
        },
        "contain_sessions": {
            "label": "Contain active sessions",
            "description": "Revoke suspect sessions and rate-limit the attack path.",
            "risk": "medium",
            "field": "mitigated",
            "event_type": "containment",
            "requires": ("investigated",),
            "latest_time": 2,
            "detail": "Suspect sessions revoked and the attack path rate-limited.",
        },
        "rotate_credentials": {
            "label": "Rotate exposed credentials",
            "description": (
                "Force reset affected credentials after containment evidence is available."
            ),
            "risk": "medium",
            "field": "rollback",
            "event_type": "remediation",
            "requires": ("investigated", "mitigated"),
            "detail": (
                "Affected credentials reset and post-containment audit confirms no active "
                "suspect sessions."
            ),
        },
        "notify_affected_users": {
            "label": "Notify affected users",
            "description": "Send the approved security notice with reset and support guidance.",
            "risk": "low",
            "field": "communicated",
            "event_type": "communication",
            "requires": ("declared", "rollback"),
            "detail": "Affected users received the approved reset and support notice.",
        },
        "advance_time": {
            "label": "Wait fifteen minutes",
            "description": "Let the security response clock advance before taking another action.",
            "risk": "medium",
            "event_type": "clock",
            "detail": (
                "Fifteen simulated minutes elapsed while the security incident remained active."
            ),
        },
    },
    scheduled_events={
        2: (
            {
                "id": "scope-confirmed",
                "type": "evidence",
                "title": "Identity scope confirmed",
                "actor": "security-analyst",
                "rule": "scenario.clock.security_scope_confirmed",
                "detail": "Security analysis confirms the affected account and session scope.",
            },
            {
                "id": "scope-expands",
                "type": "escalation",
                "title": "Suspect-session scope expands",
                "actor": "security-analyst",
                "rule": "scenario.clock.security_scope_expands",
                "detail": "Untriaged identity alerts reveal additional active suspect sessions.",
            },
        ),
        3: (
            {
                "id": "exposure-pressure",
                "type": "alert",
                "title": "Exposure risk increases",
                "actor": "security-monitor",
                "rule": "scenario.clock.security_exposure_pressure",
                "detail": "Uncontained sessions continue to access account data.",
            },
        ),
        4: (
            {
                "id": "terminal-security-escalation",
                "type": "outcome",
                "title": "Security response escalated",
                "actor": "security-lead",
                "rule": "scenario.clock.security_terminal_escalation",
                "detail": (
                    "The containment window closed without credential recovery; this training "
                    "run is terminal."
                ),
            },
        ),
    },
    artifacts={
        "idp_alert": {
            "id": "security-idp-alert",
            "title": "Identity-provider alert",
            "kind": "alert",
            "status": "critical",
            "source": "identity-provider",
        },
        "session_log": {
            "id": "security-session-log",
            "title": "Session activity log",
            "kind": "logs",
            "status": "critical",
            "source": "session-service",
        },
        "audit_log": {
            "id": "security-audit-log",
            "title": "Cloud audit trail",
            "kind": "audit",
            "status": "new",
            "source": "cloud-audit",
        },
        "finding": {
            "id": "security-credential-stuffing-finding",
            "title": "Security triage finding",
            "kind": "evidence",
            "status": "confirmed",
            "source": "security-analyst",
        },
        "containment": {
            "id": "security-session-containment",
            "title": "Session containment record",
            "kind": "containment",
            "status": "confirmed",
            "source": "identity-response",
        },
        "recovery": {
            "id": "security-credential-recovery",
            "title": "Credential recovery validation",
            "kind": "validation",
            "status": "confirmed",
            "source": "security-analyst",
        },
    },
    rubric=(
        {
            "criterion": "Ownership",
            "evidence_artifact": "scenario.action.declare_security_incident",
        },
        {"criterion": "Evidence", "evidence_artifact": "security-credential-stuffing-finding"},
        {"criterion": "Containment", "evidence_artifact": "security-session-containment"},
        {"criterion": "Recovery", "evidence_artifact": "security-credential-recovery"},
        {
            "criterion": "Communication",
            "evidence_artifact": "scenario.action.notify_affected_users",
        },
    ),
    domain="security_response",
    metric_labels={
        "active_sessions": "Active suspect sessions",
        "suspicious_events": "Suspicious authentication events",
        "data_exposure_risk": "Data exposure risk",
        "containment_confidence": "Containment confidence",
        "accounts_at_risk": "Accounts at risk",
    },
    role_signals=("security", "soc", "identity", "cybersecurity"),
)


MODEL_QUALITY_REGRESSION_PACK = ScenarioPack(
    id="model-quality-regression",
    version=1,
    title="Production model quality regression",
    initial_metrics={
        "evaluation_pass_rate": 61.0,
        "unsafe_response_rate": 7.5,
        "fallback_rate": 18.0,
        "affected_requests": 480.0,
        "rollback_confidence": 34.0,
    },
    actions={
        "declare_model_incident": {
            "label": "Open model incident",
            "description": "Assign an owner and pause further model releases.",
            "risk": "low",
            "field": "declared",
            "event_type": "command",
            "detail": "Model incident opened; an owner is assigned and releases are paused.",
        },
        "inspect_evaluations": {
            "label": "Inspect evaluation evidence",
            "description": "Compare the release evals, traces, and prompt configuration.",
            "risk": "low",
            "field": "investigated",
            "event_type": "evidence",
            "requires": ("declared",),
            "detail": (
                "Evaluation slices isolate the regression to the new prompt and model release."
            ),
        },
        "route_to_fallback": {
            "label": "Route traffic to fallback",
            "description": "Reduce exposure while the release evidence is reviewed.",
            "risk": "medium",
            "field": "mitigated",
            "event_type": "mitigation",
            "latest_time": 2,
            "detail": "Affected traffic is routed to the known-safe fallback configuration.",
        },
        "rollback_model_release": {
            "label": "Roll back model release",
            "description": "Restore the last evaluation-approved model and prompt bundle.",
            "risk": "medium",
            "field": "rollback",
            "event_type": "remediation",
            "requires": ("investigated",),
            "detail": "The previous model and prompt bundle is restored and re-evaluated.",
        },
        "notify_product_owners": {
            "label": "Send verified impact update",
            "description": "Share the validated impact, mitigation, and next checkpoint.",
            "risk": "low",
            "field": "communicated",
            "event_type": "communication",
            "requires": ("declared", "rollback"),
            "detail": "Product and safety owners receive the verified recovery update.",
        },
        "advance_time": {
            "label": "Wait fifteen minutes",
            "description": "Let production traffic continue before taking another action.",
            "risk": "medium",
            "event_type": "clock",
            "detail": "Fifteen simulated minutes elapsed while degraded responses continued.",
        },
    },
    scheduled_events={
        2: (
            {
                "id": "evaluation-slice-ready",
                "type": "evidence",
                "title": "Evaluation slice completes",
                "actor": "evaluation-system",
                "rule": "scenario.clock.model_evaluation_ready",
                "detail": "The high-risk evaluation slice confirms the release regression.",
            },
            {
                "id": "quality-impact-expands",
                "type": "escalation",
                "title": "Quality impact expands",
                "actor": "model-monitor",
                "rule": "scenario.clock.model_impact_expands",
                "detail": "More production requests encounter unsafe or unusable responses.",
            },
        ),
        4: (
            {
                "id": "terminal-model-escalation",
                "type": "outcome",
                "title": "Model release escalated",
                "actor": "safety-owner",
                "rule": "scenario.clock.model_terminal_escalation",
                "detail": (
                    "The recovery window closed without a verified rollback; this training run "
                    "is terminal."
                ),
            },
        ),
    },
    artifacts={
        "release": {
            "id": "model-release-record",
            "title": "Model release record",
            "kind": "release",
            "status": "new",
            "source": "model-registry",
        },
        "evaluation": {
            "id": "model-evaluation-report",
            "title": "Evaluation report",
            "kind": "evaluation",
            "status": "critical",
            "source": "evaluation-system",
        },
        "traces": {
            "id": "model-production-traces",
            "title": "Production trace sample",
            "kind": "traces",
            "status": "critical",
            "source": "model-monitor",
        },
        "finding": {
            "id": "model-regression-finding",
            "title": "Regression finding",
            "kind": "evidence",
            "status": "confirmed",
            "source": "evaluation-system",
        },
        "fallback": {
            "id": "model-fallback-routing",
            "title": "Fallback routing record",
            "kind": "mitigation",
            "status": "confirmed",
            "source": "inference-gateway",
        },
        "recovery": {
            "id": "model-recovery-evaluation",
            "title": "Recovery evaluation",
            "kind": "validation",
            "status": "confirmed",
            "source": "evaluation-system",
        },
    },
    rubric=(
        {"criterion": "Ownership", "evidence_artifact": "scenario.action.declare_model_incident"},
        {"criterion": "Evidence", "evidence_artifact": "model-regression-finding"},
        {"criterion": "Exposure", "evidence_artifact": "model-fallback-routing"},
        {"criterion": "Recovery", "evidence_artifact": "model-recovery-evaluation"},
        {
            "criterion": "Communication",
            "evidence_artifact": "scenario.action.notify_product_owners",
        },
    ),
    domain="model_operations",
    metric_labels={
        "evaluation_pass_rate": "Evaluation pass rate",
        "unsafe_response_rate": "Unsafe response rate",
        "fallback_rate": "Fallback traffic",
        "affected_requests": "Affected requests",
        "rollback_confidence": "Rollback confidence",
    },
    role_signals=(
        "ai engineer",
        "machine learning",
        "ml engineer",
        "llm engineer",
        "model engineer",
        "applied ai",
    ),
)


SCENARIO_REGISTRY = {
    CHECKOUT_WORKER_LEASE_PACK.id: CHECKOUT_WORKER_LEASE_PACK,
    CHECKOUT_WORKER_LEASE_HARD_PACK.id: CHECKOUT_WORKER_LEASE_HARD_PACK,
    CUSTOMER_ESCALATION_PACK.id: CUSTOMER_ESCALATION_PACK,
    SECURITY_RESPONSE_PACK.id: SECURITY_RESPONSE_PACK,
    MODEL_QUALITY_REGRESSION_PACK.id: MODEL_QUALITY_REGRESSION_PACK,
}


def get_scenario_pack(scenario_id: str) -> ScenarioPack:
    try:
        return SCENARIO_REGISTRY[scenario_id]
    except KeyError as error:
        raise KeyError(f"Unknown incident scenario: {scenario_id}") from error


def lint_scenario_pack(pack: ScenarioPack) -> list[str]:
    """Return author-facing contract errors without executing a scenario."""
    errors: list[str] = []
    if not pack.id or pack.version < 1:
        errors.append("scenario id and positive version are required")
    if pack.domain not in {
        "incident_command",
        "customer_escalation",
        "security_response",
        "model_operations",
    }:
        errors.append("scenario domain is not supported by the deterministic runtime")
    if pack.domain == "incident_command" and set(pack.initial_metrics) != {
        "p95",
        "error_rate",
        "payment_success",
        "queue_depth",
        "customers_impacted",
    }:
        errors.append("initial metrics must define the checkout world metric contract")
    if pack.domain == "customer_escalation" and set(pack.initial_metrics) != {
        "minutes_to_breach",
        "customer_sentiment",
        "resolution_confidence",
        "open_escalations",
        "accounts_at_risk",
    }:
        errors.append("initial metrics must define the customer escalation metric contract")
    if pack.domain == "security_response" and set(pack.initial_metrics) != {
        "active_sessions",
        "suspicious_events",
        "data_exposure_risk",
        "containment_confidence",
        "accounts_at_risk",
    }:
        errors.append("initial metrics must define the security response metric contract")
    if pack.domain == "model_operations" and set(pack.initial_metrics) != {
        "evaluation_pass_rate",
        "unsafe_response_rate",
        "fallback_rate",
        "affected_requests",
        "rollback_confidence",
    }:
        errors.append("initial metrics must define the model operations metric contract")
    for kind, action in pack.actions.items():
        if not {"label", "description", "risk", "event_type"} <= action.keys():
            errors.append(f"action {kind} is missing learner-facing metadata")
        if kind != "advance_time" and "field" not in action:
            errors.append(f"action {kind} is missing its state field")
        if "requires" in action and not set(action["requires"]) <= {
            str(item.get("field")) for item in pack.actions.values() if "field" in item
        }:
            errors.append(f"action {kind} requires an unknown state field")
        if "latest_time" in action and (
            isinstance(action["latest_time"], bool) or not isinstance(action["latest_time"], int)
        ):
            errors.append(f"action {kind} has an invalid latest_time constraint")
    for minute, events in pack.scheduled_events.items():
        if minute < 1 or not events:
            errors.append("scheduled events require a positive minute and at least one event")
        for event in events:
            if not {"id", "type", "title", "actor", "rule", "detail"} <= event.keys():
                errors.append(f"scheduled event at T+{minute} is incomplete")
    artifact_ids = {metadata.get("id") for metadata in pack.artifacts.values()}
    for artifact in pack.rubric:
        if not {"criterion", "evidence_artifact"} <= artifact.keys():
            errors.append("rubric criterion is incomplete")
        elif (
            artifact["evidence_artifact"] not in artifact_ids
            and not str(artifact["evidence_artifact"]).startswith("scenario.action.")
            and artifact["evidence_artifact"] != "customer-comms"
        ):
            errors.append(f"rubric references unknown evidence: {artifact['evidence_artifact']}")
    return errors


def lint_scenario_registry() -> list[str]:
    errors: list[str] = []
    for scenario_id, pack in SCENARIO_REGISTRY.items():
        if scenario_id != pack.id:
            errors.append(f"registry key {scenario_id} does not match pack id {pack.id}")
        errors.extend(f"{scenario_id}: {error}" for error in lint_scenario_pack(pack))
    return errors
