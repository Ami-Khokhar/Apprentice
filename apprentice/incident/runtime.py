"""A deterministic, persisted Incident Command world.

The scenario engine has no wall-clock or model inputs.  SQLite records the
canonical world after every causal event, which makes a run durable and lets
the UI reconstruct the exact state at any event index.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from demo_incident_service.scenario import (
    SCENARIO_ID as FIXTURE_SCENARIO_ID,
)
from demo_incident_service.scenario import (
    SCHEMA_VERSION as FIXTURE_SCHEMA_VERSION,
)
from demo_incident_service.scenario import (
    export_worker_lease_fixture,
)

from .actors import InvalidActorRequestError, actor_view, delegation_policy
from .scenario_pack import CHECKOUT_WORKER_LEASE_PACK, get_scenario_pack

if TYPE_CHECKING:
    from apprentice.ledger.db import SQLiteDatabase


class IncidentNotFoundError(KeyError):
    pass


class InvalidIncidentActionError(ValueError):
    pass


DEFAULT_SCENARIO_ID = CHECKOUT_WORKER_LEASE_PACK.id
SCENARIO_VERSION = CHECKOUT_WORKER_LEASE_PACK.version


@dataclass
class Incident:
    id: str
    scenario_id: str = DEFAULT_SCENARIO_ID
    scenario_version: int = SCENARIO_VERSION
    fixture_schema_version: int = FIXTURE_SCHEMA_VERSION
    fixture_title: str = ""
    seed: int = 0
    time: int = 0
    declared: bool = False
    investigated: bool = False
    rollback: bool = False
    mitigated: bool = False
    communicated: bool = False
    terminal: bool = False
    terminal_reason: str | None = None
    metrics: dict[str, float] = field(
        default_factory=lambda: deepcopy(CHECKOUT_WORKER_LEASE_PACK.initial_metrics)
    )
    events: list[dict[str, object]] = field(default_factory=list)


class IncidentRuntime:
    """Scenario store; pass the ledger's SQLite database for durable runs.

    The no-argument form remains useful for small, isolated deterministic unit
    tests.  The sidecar always supplies its file-backed database.
    """

    def __init__(self, database: SQLiteDatabase | None = None) -> None:
        self._database = database
        self._incidents: dict[str, Incident] = {}

    def create(self, *, scenario_id: str = DEFAULT_SCENARIO_ID, seed: int = 0) -> dict[str, object]:
        try:
            pack = get_scenario_pack(scenario_id)
        except KeyError as error:
            raise InvalidIncidentActionError(f"Unknown incident scenario: {scenario_id}") from error
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise InvalidIncidentActionError("Scenario seed must be an integer")
        fixture = self._fixture() if pack.domain == "incident_command" else None
        incident = Incident(
            id=str(uuid4()),
            scenario_id=scenario_id,
            scenario_version=pack.version,
            fixture_schema_version=int(fixture["schema_version"]) if fixture else 0,
            fixture_title=str(fixture["title"]) if fixture else pack.title,
            seed=seed,
            metrics=deepcopy(pack.initial_metrics),
        )
        self._append(
            incident,
            type="alert",
            title="Checkout latency alert fired"
            if fixture
            else "Enterprise customer escalation opened"
            if pack.domain == "customer_escalation"
            else "Production model quality alert fired"
            if pack.domain == "model_operations"
            else "Credential-stuffing alert fired",
            detail=str(fixture["title"])
            if fixture
            else "Renewal sponsor reports an unresolved provisioning failure."
            if pack.domain == "customer_escalation"
            else (
                "Production evaluations and trace sampling show a sharp quality regression "
                "after a model and prompt release."
            )
            if pack.domain == "model_operations"
            else "Identity-provider telemetry detected a coordinated credential-stuffing campaign.",
            rule="scenario.initial_alert",
            delta={},
        )
        self._save_run(incident, create=True)
        return self._snapshot(incident)

    def snapshot(self, incident_id: str) -> dict[str, object]:
        return self._snapshot(self._get(incident_id))

    def apply(self, incident_id: str, kind: str) -> dict[str, object]:
        incident = self._get(incident_id)
        pack = get_scenario_pack(incident.scenario_id)
        if kind not in pack.actions:
            raise InvalidIncidentActionError(f"Unknown incident action: {kind}")
        if incident.terminal:
            raise InvalidIncidentActionError(
                "The incident has reached a terminal escalation outcome"
            )
        if self._completed(incident):
            raise InvalidIncidentActionError("The incident is already recovered")
        if not self._enabled(incident, kind):
            raise InvalidIncidentActionError(f"Action is not available now: {kind}")

        before = deepcopy(incident.metrics)
        action = pack.actions[kind]
        if kind != "advance_time":
            setattr(incident, str(action["field"]), True)
        self._advance(incident)
        detail = str(action.get("detail", ""))
        if action.get("detail_from_fixture") == "remediation.expected_effect":
            detail = str(self._fixture()["remediation"]["expected_effect"])
        self._append(
            incident,
            type=str(action["event_type"]),
            title=str(action["label"]),
            detail=detail,
            rule=f"scenario.action.{kind}",
            delta=self._metric_delta(before, incident.metrics),
        )
        self._emit_scheduled_events(incident)
        self._save_run(incident)
        return self._snapshot(incident)

    def delegate(self, incident_id: str, actor_id: str, request: str) -> dict[str, object]:
        """Record a bounded actor request without granting it world authority.

        Delegation is intentionally informational in this initial pack.  The
        actor can neither advance time nor alter metrics; any future exception
        must be represented by an explicit policy and runtime transition.
        """
        incident = self._get(incident_id)
        if incident.terminal:
            raise InvalidIncidentActionError(
                "The incident has reached a terminal escalation outcome"
            )
        try:
            policy = delegation_policy(actor_id, request)
        except InvalidActorRequestError as error:
            raise InvalidIncidentActionError(str(error)) from error
        before = deepcopy(incident.metrics)
        if policy["metric_effect"] != "none":
            raise RuntimeError("Actor metric effects require an explicit runtime transition")
        self._append(
            incident,
            type="actor_request",
            title=policy["title"],
            detail=policy["detail"],
            rule=policy["rule"],
            delta=self._metric_delta(before, incident.metrics),
            actor=actor_id,
            request=request,
        )
        self._save_run(incident)
        return self._snapshot(incident)

    def actor_view(self, incident_id: str, actor_id: str) -> dict[str, object]:
        incident = self._get(incident_id)
        return self._actor_view(incident, actor_id)

    def replay(self, incident_id: str) -> list[dict[str, object]]:
        return deepcopy(
            [
                {key: value for key, value in event.items() if key != "snapshot"}
                for event in self._get(incident_id).events
            ]
        )

    def replay_snapshot(self, incident_id: str, event_index: int) -> dict[str, object]:
        incident = self._get(incident_id)
        if event_index < 0 or event_index >= len(incident.events):
            raise InvalidIncidentActionError(f"Replay event index is out of range: {event_index}")
        if self._database is None:
            # The in-memory mode is only for tests; replay by re-applying its
            # own stored state cannot be reconstructed after mutation, so use
            # the snapshots captured alongside each event.
            return deepcopy(incident.events[event_index]["snapshot"])
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM incident_events "
                "WHERE incident_id = ? AND event_index = ?",
                (incident_id, event_index),
            ).fetchone()
        if row is None:
            raise IncidentNotFoundError(f"Unknown incident replay event: {event_index}")
        return json.loads(row["snapshot_json"])

    def _advance(self, incident: Incident) -> None:
        incident.time += 1
        if self._pack(incident).domain == "model_operations":
            metrics = incident.metrics
            if incident.rollback:
                metrics["evaluation_pass_rate"] = 98.0
                metrics["unsafe_response_rate"] = 0.4
                metrics["fallback_rate"] = 0.0
                metrics["rollback_confidence"] = 97.0
            elif incident.mitigated:
                metrics["unsafe_response_rate"] = max(0.0, metrics["unsafe_response_rate"] - 4.0)
                metrics["fallback_rate"] = min(100.0, metrics["fallback_rate"] + 52.0)
                metrics["affected_requests"] += 80.0
                metrics["rollback_confidence"] = min(100.0, metrics["rollback_confidence"] + 14.0)
            else:
                metrics["evaluation_pass_rate"] = max(0.0, metrics["evaluation_pass_rate"] - 5.0)
                metrics["unsafe_response_rate"] = min(100.0, metrics["unsafe_response_rate"] + 2.5)
                metrics["affected_requests"] += 260.0
                metrics["rollback_confidence"] = max(0.0, metrics["rollback_confidence"] - 3.0)
            return
        if self._pack(incident).domain == "security_response":
            metrics = incident.metrics
            if incident.rollback:
                metrics["active_sessions"] = 0.0
                metrics["suspicious_events"] = 4.0
                metrics["data_exposure_risk"] = 4.0
                metrics["containment_confidence"] = 96.0
                metrics["accounts_at_risk"] = 0.0
            elif incident.mitigated:
                metrics["active_sessions"] = max(0.0, metrics["active_sessions"] - 24.0)
                metrics["suspicious_events"] = max(0.0, metrics["suspicious_events"] - 72.0)
                metrics["data_exposure_risk"] = max(0.0, metrics["data_exposure_risk"] - 18.0)
                metrics["containment_confidence"] = min(
                    100.0, metrics["containment_confidence"] + 52.0
                )
                metrics["accounts_at_risk"] = max(0.0, metrics["accounts_at_risk"] - 18.0)
            else:
                metrics["active_sessions"] += 14.0
                metrics["suspicious_events"] += 62.0
                metrics["data_exposure_risk"] = min(100.0, metrics["data_exposure_risk"] + 12.0)
                metrics["containment_confidence"] = max(
                    0.0, metrics["containment_confidence"] - 4.0
                )
                metrics["accounts_at_risk"] += 14.0
            return
        if self._pack(incident).domain == "customer_escalation":
            metrics = incident.metrics
            if incident.rollback:
                metrics["minutes_to_breach"] = 0.0
                metrics["customer_sentiment"] = min(100.0, metrics["customer_sentiment"] + 20.0)
                metrics["resolution_confidence"] = 92.0
                metrics["open_escalations"] = 0.0
                metrics["accounts_at_risk"] = 0.0
            else:
                metrics["minutes_to_breach"] = max(0.0, metrics["minutes_to_breach"] - 15.0)
                metrics["customer_sentiment"] = max(0.0, metrics["customer_sentiment"] - 8.0)
                metrics["resolution_confidence"] = max(0.0, metrics["resolution_confidence"] - 4.0)
            return
        metrics = incident.metrics
        if incident.rollback:
            metrics["error_rate"] = 0.3
            metrics["payment_success"] = 99.7
            metrics["p95"] = 330.0
            metrics["queue_depth"] = max(0.0, metrics["queue_depth"] - 1200.0)
            return
        queue_growth = 180.0 if incident.mitigated else 420.0
        metrics["queue_depth"] += queue_growth
        error_growth = 0.4 if incident.mitigated else 1.5
        metrics["error_rate"] = min(18.0, metrics["error_rate"] + error_growth)
        metrics["payment_success"] = 100.0 - metrics["error_rate"]
        metrics["p95"] = min(6000.0, metrics["p95"] + (100.0 if incident.mitigated else 380.0))
        metrics["customers_impacted"] += 12.0 if incident.mitigated else 35.0

    def _emit_scheduled_events(self, incident: Incident) -> None:
        """Inject scenario-clock events after the learner's event at a minute."""
        if self._pack(incident).domain == "model_operations":
            scheduled = self._pack(incident).scheduled_events
            if incident.time == 2 and not incident.rollback:
                before = deepcopy(incident.metrics)
                if incident.investigated:
                    event = scheduled[2][0]
                    delta: dict[str, int | float] = {}
                else:
                    incident.metrics["affected_requests"] += 220.0
                    incident.metrics["unsafe_response_rate"] = min(
                        100.0, incident.metrics["unsafe_response_rate"] + 2.0
                    )
                    event = scheduled[2][1]
                    delta = self._metric_delta(before, incident.metrics)
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta=delta,
                    actor=event["actor"],
                )
            if incident.time == 4 and not incident.rollback:
                event = scheduled[4][0]
                incident.terminal = True
                incident.terminal_reason = "model quality incident escalated without rollback"
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta={},
                    actor=event["actor"],
                )
            return
        if self._pack(incident).domain == "security_response":
            scheduled = self._pack(incident).scheduled_events
            if incident.time == 2 and not incident.rollback:
                before = deepcopy(incident.metrics)
                if incident.investigated:
                    event = scheduled[2][0]
                    delta: dict[str, int | float] = {}
                else:
                    incident.metrics["active_sessions"] += 18.0
                    incident.metrics["accounts_at_risk"] += 18.0
                    incident.metrics["data_exposure_risk"] = min(
                        100.0, incident.metrics["data_exposure_risk"] + 10.0
                    )
                    event = scheduled[2][1]
                    delta = self._metric_delta(before, incident.metrics)
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta=delta,
                    actor=event["actor"],
                )
            if incident.time == 3 and not incident.rollback and not incident.mitigated:
                before = deepcopy(incident.metrics)
                incident.metrics["data_exposure_risk"] = min(
                    100.0, incident.metrics["data_exposure_risk"] + 15.0
                )
                event = scheduled[3][0]
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta=self._metric_delta(before, incident.metrics),
                    actor=event["actor"],
                )
            if incident.time == 4 and not incident.rollback:
                event = scheduled[4][0]
                incident.terminal = True
                incident.terminal_reason = (
                    "security incident escalated without containment and credential recovery"
                )
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta={},
                    actor=event["actor"],
                )
            return
        if self._pack(incident).domain == "customer_escalation":
            scheduled = self._pack(incident).scheduled_events
            if incident.time == 2 and not incident.rollback:
                event = scheduled[2][0]
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta={},
                    actor=event["actor"],
                )
            if incident.time == 4 and not incident.rollback:
                event = scheduled[4][0]
                incident.terminal = True
                incident.terminal_reason = (
                    "customer renewal decision escalated without a confirmed remedy"
                )
                self._append(
                    incident,
                    type=event["type"],
                    title=event["title"],
                    detail=event["detail"],
                    rule=event["rule"],
                    delta={},
                    actor=event["actor"],
                )
            return
        scheduled = get_scenario_pack(incident.scenario_id).scheduled_events
        if incident.time == 2 and not incident.rollback:
            before = deepcopy(incident.metrics)
            if incident.declared:
                event = scheduled[2][0]
                delta: dict[str, int | float] = {}
            else:
                incident.metrics["p95"] = min(6000.0, incident.metrics["p95"] + 250.0)
                incident.metrics["customers_impacted"] += 50.0
                event = scheduled[2][1]
                delta = self._metric_delta(before, incident.metrics)
            self._append(
                incident,
                type=event["type"],
                title=event["title"],
                detail=event["detail"],
                rule=event["rule"],
                delta=delta,
                actor=event["actor"],
            )
        if incident.time == 3 and not incident.rollback:
            before = deepcopy(incident.metrics)
            incident.metrics["customers_impacted"] += 60.0
            ticket = scheduled[3][0]
            self._append(
                incident,
                type=ticket["type"],
                title=ticket["title"],
                detail=ticket["detail"],
                rule=ticket["rule"],
                delta=self._metric_delta(before, incident.metrics),
                actor=ticket["actor"],
            )
            comms = scheduled[3][1] if incident.communicated else scheduled[3][2]
            self._append(
                incident,
                type=comms["type"],
                title=comms["title"],
                detail=comms["detail"],
                rule=comms["rule"],
                delta={},
                actor=comms["actor"],
            )
        if incident.time == 4 and not incident.rollback:
            terminal = scheduled[4][0]
            incident.terminal = True
            incident.terminal_reason = (
                "checkout escalation transferred to executive incident response"
            )
            self._append(
                incident,
                type=terminal["type"],
                title=terminal["title"],
                detail=terminal["detail"],
                rule=terminal["rule"],
                delta={},
                actor=terminal["actor"],
            )

    def _snapshot(self, incident: Incident) -> dict[str, object]:
        pack = self._pack(incident)
        completed = self._completed(incident)
        outcome = (
            "recovered" if completed else "terminal_escalation" if incident.terminal else "active"
        )
        artifacts = self._artifacts(incident)
        return {
            "id": incident.id,
            "scenario": incident.scenario_id,
            "scenario_version": incident.scenario_version,
            "fixture_schema_version": incident.fixture_schema_version,
            "fixture_title": incident.fixture_title,
            "seed": incident.seed,
            "sim_time": incident.time,
            "severity": (
                ("Fallback active" if incident.mitigated else "Quality degraded")
                if pack.domain == "model_operations"
                else ("Contained" if incident.mitigated else "Uncontained")
                if pack.domain == "security_response"
                else ("Owned" if incident.declared else "Unassigned")
                if pack.domain == "customer_escalation"
                else "SEV-2"
                if incident.declared
                else "Undeclared"
            ),
            "metrics": {key: self._number(value) for key, value in incident.metrics.items()},
            "metric_labels": deepcopy(pack.metric_labels or {}),
            "artifacts": artifacts,
            "actors": [
                self._actor_view(incident, actor_id, artifacts)
                for actor_id in ("sre-oncall", "support-lead", "customer-comms")
            ]
            if pack.domain == "incident_command"
            else [],
            "events": deepcopy(
                [
                    {key: value for key, value in event.items() if key != "snapshot"}
                    for event in incident.events
                ]
            ),
            "available_actions": self._available_actions(incident, completed),
            "completed": completed,
            "terminal": incident.terminal,
            "outcome": outcome,
            "debrief": self._debrief(incident, completed),
        }

    def _actor_view(
        self,
        incident: Incident,
        actor_id: str,
        artifacts: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return actor_view(
            actor_id,
            sim_time=incident.time,
            declared=incident.declared,
            investigated=incident.investigated,
            rollback=incident.rollback,
            communicated=incident.communicated,
            metrics=incident.metrics,
            artifacts=self._artifacts(incident) if artifacts is None else artifacts,
        )

    def _artifacts(self, incident: Incident) -> list[dict[str, object]]:
        if self._pack(incident).domain == "model_operations":
            return self._model_artifacts(incident)
        if self._pack(incident).domain == "security_response":
            return self._security_artifacts(incident)
        if self._pack(incident).domain == "customer_escalation":
            return self._customer_artifacts(incident)
        fixture = self._fixture()
        artifact = get_scenario_pack(incident.scenario_id).artifacts
        failure = fixture["failure"]
        assert isinstance(failure, dict)
        deployment = failure["deployment"]
        logs = failure["logs"]
        metrics = failure["metrics"]
        assert isinstance(deployment, dict)
        assert isinstance(logs, list)
        assert isinstance(metrics, dict)
        artifacts = [
            {
                **artifact["failure_deployment"],
                "timestamp": "T-4",
                "visibility": "incident-command",
                "version": str(fixture["schema_version"]),
                "content": json.dumps(deployment, sort_keys=True),
                "evidence": deepcopy(deployment),
            },
            {
                **artifact["failure_rejection_log"],
                "timestamp": "T+0",
                "visibility": "incident-command",
                "version": str(fixture["schema_version"]),
                "content": json.dumps(logs[-1], sort_keys=True),
                "evidence": deepcopy(logs[-1]),
            },
            {
                **artifact["failure_metrics"],
                "timestamp": f"T+{incident.time}",
                "visibility": "incident-command",
                "version": str(fixture["schema_version"]),
                "content": (
                    f"fixture backlog: {metrics['checkout_backlog']}; "
                    f"worker leases: {metrics['checkout_worker_leases']}/"
                    f"{metrics['checkout_worker_capacity']}; simulated queue: "
                    f"{int(incident.metrics['queue_depth'])}."
                ),
                "evidence": deepcopy(metrics),
            },
        ]
        if incident.investigated:
            artifacts.append(
                {
                    **artifact["investigation"],
                    "timestamp": f"T+{self._action_time(incident, 'investigate')}",
                    "visibility": "incident-command",
                    "version": "finding-v1",
                    "content": (
                        "The new worker concurrency setting starves payment authorization retries."
                    ),
                }
            )
        if incident.time >= 3:
            artifacts.append(
                {
                    **artifact["support_surge"],
                    "timestamp": "T+3",
                    "visibility": "incident-command",
                    "version": "report-v1",
                    "content": (
                        "Support contact volume rose as failed checkout attempts accumulated."
                    ),
                }
            )
        if incident.rollback:
            recovery = fixture["recovery"]
            assert isinstance(recovery, dict)
            artifacts.append(
                {
                    **artifact["recovery"],
                    "timestamp": f"T+{incident.time}",
                    "visibility": "incident-command",
                    "version": str(fixture["schema_version"]),
                    "content": json.dumps(recovery, sort_keys=True),
                    "evidence": deepcopy(recovery),
                }
            )
        return artifacts

    def _available_actions(self, incident: Incident, completed: bool) -> list[dict[str, object]]:
        actions = get_scenario_pack(incident.scenario_id).actions
        return [
            {
                "kind": kind,
                "label": label,
                "description": description,
                "risk": risk,
                "enabled": not completed
                and not incident.terminal
                and self._enabled(incident, kind),
            }
            for kind, action in actions.items()
            for label, description, risk in [
                (str(action["label"]), str(action["description"]), str(action["risk"]))
            ]
        ]

    def _enabled(self, incident: Incident, kind: str) -> bool:
        action = get_scenario_pack(incident.scenario_id).actions[kind]
        if any(not getattr(incident, field) for field in action.get("requires", ())):
            return False
        if incident.time > int(action.get("latest_time", incident.time)):
            return False
        if self._pack(incident).domain == "model_operations":
            if kind == "declare_model_incident":
                return not incident.declared
            if kind == "inspect_evaluations":
                return not incident.investigated
            if kind == "route_to_fallback":
                return not incident.mitigated and not incident.rollback
            if kind == "rollback_model_release":
                return not incident.rollback
            if kind == "notify_product_owners":
                return not incident.communicated
            return kind == "advance_time"
        if self._pack(incident).domain == "customer_escalation":
            if kind == "acknowledge_escalation":
                return not incident.declared
            if kind == "review_case":
                return not incident.investigated
            if kind == "approve_remedy":
                return not incident.rollback
            if kind == "send_resolution":
                return not incident.communicated
            return kind == "advance_time"
        if self._pack(incident).domain == "security_response":
            if kind == "declare_security_incident":
                return not incident.declared
            if kind == "triage_alert":
                return not incident.investigated
            if kind == "contain_sessions":
                return not incident.mitigated and not incident.rollback
            if kind == "rotate_credentials":
                return not incident.rollback
            if kind == "notify_affected_users":
                return not incident.communicated
            return kind == "advance_time"
        if kind == "declare_incident":
            return not incident.declared
        if kind == "investigate":
            return not incident.investigated
        if kind == "rollback":
            return not incident.rollback
        if kind == "mitigate":
            return not incident.mitigated and not incident.rollback and incident.time < 2
        if kind == "communicate":
            return incident.declared and not incident.communicated
        return kind == "advance_time"

    def _debrief(self, incident: Incident, completed: bool) -> list[dict[str, object]]:
        if self._pack(incident).domain == "model_operations":
            return self._model_debrief(incident, completed)
        if self._pack(incident).domain == "security_response":
            return self._security_debrief(incident, completed)
        if self._pack(incident).domain == "customer_escalation":
            return self._customer_debrief(incident, completed)
        rubric = get_scenario_pack(incident.scenario_id).rubric
        declared_time = self._action_time(incident, "declare_incident")
        investigated_event = self._event_index(incident, "scenario.action.investigate")
        rollback_event = self._event_index(incident, "scenario.action.rollback")
        communication_event = self._event_index(incident, "scenario.action.communicate")
        return [
            {
                "criterion": rubric[0]["criterion"],
                "detail": "Severity declared before the T+2 escalation window."
                if declared_time is not None and declared_time < 2
                else "Incident command was absent or declared after escalation.",
                "score": int(declared_time is not None and declared_time < 2),
                "evidence": self._links(declared_time, "scenario.action.declare_incident"),
            },
            {
                "criterion": rubric[1]["criterion"],
                "detail": "Deployment and logs were reviewed."
                if incident.investigated
                else "Rollback or mitigation happened without reviewing the available evidence.",
                "score": 1 if incident.investigated else 0,
                "evidence": self._links(investigated_event, "finding-worker-concurrency"),
            },
            {
                "criterion": rubric[2]["criterion"],
                "detail": "The known-good checkout release was restored."
                if incident.rollback
                else "No rollback restored the known-good release.",
                "score": 1 if incident.rollback else 0,
                "evidence": self._links(rollback_event, "fixture-failure-deployment"),
            },
            {
                "criterion": rubric[3]["criterion"],
                "detail": "A status update was published."
                if incident.communicated
                else "No customer-facing update was recorded.",
                "score": 1 if incident.communicated else 0,
                "evidence": self._links(communication_event, "customer-comms"),
            },
            {
                "criterion": rubric[4]["criterion"],
                "detail": "Queue drained and checkout metrics returned to the healthy range."
                if completed
                else "Recovery has not yet been validated by a drained queue.",
                "score": 1 if completed else 0,
                "evidence": self._links(rollback_event, "fixture-recovery-validation"),
            },
        ]

    @staticmethod
    def _links(event_index: int | None, artifact_id: str) -> list[dict[str, object]]:
        links: list[dict[str, object]] = [{"artifact_id": artifact_id}]
        if event_index is not None:
            links.insert(0, {"event_index": event_index})
        return links

    @staticmethod
    def _event_index(incident: Incident, rule: str) -> int | None:
        return next(
            (int(event["event_index"]) for event in incident.events if event["rule"] == rule),
            None,
        )

    @classmethod
    def _action_time(cls, incident: Incident, kind: str) -> int | None:
        index = cls._event_index(incident, f"scenario.action.{kind}")
        if index is None:
            return None
        return int(incident.events[index]["time"])

    @staticmethod
    def _fixture() -> dict[str, object]:
        """Load plain fixture data only; never call the service over HTTP."""
        fixture = export_worker_lease_fixture()
        if (
            fixture.get("scenario_id") != FIXTURE_SCENARIO_ID
            or fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION
        ):
            raise RuntimeError("Worker-lease fixture contract is incompatible with this scenario")
        return fixture

    @staticmethod
    def _number(value: float) -> int | float:
        return int(value) if value.is_integer() else round(value, 1)

    @classmethod
    def _metric_delta(
        cls, before: dict[str, float], after: dict[str, float]
    ) -> dict[str, int | float]:
        return {
            name: cls._number(value - before[name])
            for name, value in after.items()
            if value != before[name]
        }

    def _append(self, incident: Incident, **event: object) -> None:
        event_index = len(incident.events)
        incident.events.append({"event_index": event_index, "time": incident.time, **event})
        # In-memory runtime also holds immutable historical snapshots.
        incident.events[-1]["snapshot"] = self._snapshot(incident)

    def _save_run(self, incident: Incident, *, create: bool = False) -> None:
        if self._database is None:
            self._incidents[incident.id] = incident
            return
        state = asdict(incident)
        state["events"] = []
        now = time.time()
        with self._database.transaction(write=True) as connection:
            if create:
                connection.execute(
                    "INSERT INTO incident_runs "
                    "(id, scenario_id, scenario_version, seed, state_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        incident.id,
                        incident.scenario_id,
                        incident.scenario_version,
                        incident.seed,
                        json.dumps(state, sort_keys=True),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE incident_runs SET state_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(state, sort_keys=True), now, incident.id),
                )
            connection.executemany(
                "INSERT OR IGNORE INTO incident_events "
                "(incident_id, event_index, event_json, snapshot_json) VALUES (?, ?, ?, ?)",
                [
                    (
                        incident.id,
                        event["event_index"],
                        json.dumps(
                            {key: value for key, value in event.items() if key != "snapshot"},
                            sort_keys=True,
                        ),
                        json.dumps(event["snapshot"], sort_keys=True),
                    )
                    for event in incident.events
                ],
            )

    def _get(self, incident_id: str) -> Incident:
        if self._database is None:
            try:
                return self._incidents[incident_id]
            except KeyError as error:
                raise IncidentNotFoundError(f"Unknown incident: {incident_id}") from error
        with self._database.transaction() as connection:
            run = connection.execute(
                "SELECT state_json FROM incident_runs WHERE id = ?", (incident_id,)
            ).fetchone()
            rows = connection.execute(
                "SELECT event_json, snapshot_json FROM incident_events "
                "WHERE incident_id = ? ORDER BY event_index",
                (incident_id,),
            ).fetchall()
        if run is None:
            raise IncidentNotFoundError(f"Unknown incident: {incident_id}")
        state = json.loads(run["state_json"])
        state["events"] = [
            {**json.loads(row["event_json"]), "snapshot": json.loads(row["snapshot_json"])}
            for row in rows
        ]
        return Incident(**state)

    @staticmethod
    def _pack(incident: Incident):
        return get_scenario_pack(incident.scenario_id)

    def _completed(self, incident: Incident) -> bool:
        if self._pack(incident).domain == "model_operations":
            return (
                incident.rollback
                and incident.communicated
                and incident.metrics["evaluation_pass_rate"] >= 95.0
            )
        if self._pack(incident).domain == "security_response":
            return (
                incident.rollback
                and incident.communicated
                and incident.metrics["active_sessions"] == 0
            )
        if self._pack(incident).domain == "customer_escalation":
            return incident.rollback and incident.communicated
        return incident.rollback and incident.metrics["queue_depth"] == 0

    def _model_artifacts(self, incident: Incident) -> list[dict[str, object]]:
        artifacts = self._pack(incident).artifacts
        result = [
            {
                **artifacts["release"],
                "timestamp": "T-15",
                "visibility": "model-operations",
                "version": "v1",
                "content": "A new model and prompt bundle reached production fifteen minutes ago.",
            },
            {
                **artifacts["evaluation"],
                "timestamp": "T+0",
                "visibility": "model-operations",
                "version": "v1",
                "content": "The production evaluation pass rate fell from 96% to 61%.",
            },
            {
                **artifacts["traces"],
                "timestamp": f"T+{incident.time}",
                "visibility": "model-operations",
                "version": "v1",
                "content": (
                    f"Trace sampling reports {incident.metrics['unsafe_response_rate']:.1f}% "
                    "unsafe responses in the affected evaluation slice."
                ),
            },
        ]
        if incident.investigated:
            result.append(
                {
                    **artifacts["finding"],
                    "timestamp": f"T+{self._action_time(incident, 'inspect_evaluations')}",
                    "visibility": "model-operations",
                    "version": "v1",
                    "content": (
                        "Release comparison isolates the regression to the new prompt and model "
                        "bundle; the prior bundle passes the affected slice."
                    ),
                }
            )
        if incident.mitigated:
            result.append(
                {
                    **artifacts["fallback"],
                    "timestamp": f"T+{self._action_time(incident, 'route_to_fallback')}",
                    "visibility": "model-operations",
                    "version": "v1",
                    "content": "Affected traffic is routed to the known-safe fallback bundle.",
                }
            )
        if incident.rollback:
            result.append(
                {
                    **artifacts["recovery"],
                    "timestamp": f"T+{self._action_time(incident, 'rollback_model_release')}",
                    "visibility": "model-operations",
                    "version": "v1",
                    "content": "The restored bundle passes 98% of the recovery evaluation.",
                }
            )
        return result

    def _model_debrief(self, incident: Incident, completed: bool) -> list[dict[str, object]]:
        rubric = self._pack(incident).rubric
        checks = (
            (incident.declared, "Model-incident ownership was assigned and releases were paused."),
            (incident.investigated, "Evaluation slices and traces established the regression."),
            (incident.mitigated, "Fallback routing reduced exposure while diagnosis continued."),
            (incident.rollback, "The approved model and prompt bundle was restored and evaluated."),
            (completed, "Product and safety owners received the verified recovery update."),
        )
        evidence = (
            "scenario.action.declare_model_incident",
            "model-regression-finding",
            "model-fallback-routing",
            "model-recovery-evaluation",
            "scenario.action.notify_product_owners",
        )
        return [
            {
                "criterion": item["criterion"],
                "detail": detail,
                "score": int(ok),
                "evidence": self._links(self._event_index(incident, evidence_item), evidence_item),
            }
            for item, (ok, detail), evidence_item in zip(rubric, checks, evidence, strict=True)
        ]

    def _security_artifacts(self, incident: Incident) -> list[dict[str, object]]:
        artifacts = self._pack(incident).artifacts
        result = [
            {
                **artifacts["idp_alert"],
                "timestamp": "T+0",
                "visibility": "security-response",
                "version": "v1",
                "content": (
                    "148 failed authentication attempts from a coordinated credential-stuffing "
                    "pattern."
                ),
            },
            {
                **artifacts["session_log"],
                "timestamp": f"T+{incident.time}",
                "visibility": "security-response",
                "version": "v1",
                "content": (
                    f"{int(incident.metrics['active_sessions'])} suspect sessions remain active."
                ),
            },
            {
                **artifacts["audit_log"],
                "timestamp": "T+0",
                "visibility": "security-response",
                "version": "v1",
                "content": (
                    "Audit trail shows successful logins following repeated credential failures; "
                    "no privilege elevation observed."
                ),
            },
        ]
        if incident.investigated:
            result.append(
                {
                    **artifacts["finding"],
                    "timestamp": f"T+{self._action_time(incident, 'triage_alert')}",
                    "visibility": "security-response",
                    "version": "v1",
                    "content": (
                        "Automated credential stuffing reused leaked passwords; affected sessions "
                        "require revocation and credential reset."
                    ),
                }
            )
        if incident.mitigated:
            result.append(
                {
                    **artifacts["containment"],
                    "timestamp": f"T+{self._action_time(incident, 'contain_sessions')}",
                    "visibility": "security-response",
                    "version": "v1",
                    "content": (
                        "Suspect sessions revoked and attack traffic rate-limited at the identity "
                        "edge."
                    ),
                }
            )
        if incident.rollback:
            result.append(
                {
                    **artifacts["recovery"],
                    "timestamp": f"T+{self._action_time(incident, 'rotate_credentials')}",
                    "visibility": "security-response",
                    "version": "v1",
                    "content": (
                        "Affected credentials reset; post-reset audit finds no active suspect "
                        "sessions."
                    ),
                }
            )
        return result

    def _security_debrief(self, incident: Incident, completed: bool) -> list[dict[str, object]]:
        rubric = self._pack(incident).rubric
        checks = (
            (
                incident.declared,
                "Security ownership was assigned before the scope expansion window.",
            ),
            (
                incident.investigated,
                "Identity, session, and audit evidence established the attack pattern.",
            ),
            (
                incident.mitigated,
                "Suspect sessions were revoked and the attack path was rate-limited.",
            ),
            (
                incident.rollback,
                "Affected credentials were reset and recovery evidence was captured.",
            ),
            (completed, "Affected users received the approved notice after credential recovery."),
        )
        evidence = (
            "scenario.action.declare_security_incident",
            "security-credential-stuffing-finding",
            "security-session-containment",
            "security-credential-recovery",
            "scenario.action.notify_affected_users",
        )
        return [
            {
                "criterion": item["criterion"],
                "detail": detail,
                "score": int(ok),
                "evidence": self._links(self._event_index(incident, evidence_item), evidence_item),
            }
            for item, (ok, detail), evidence_item in zip(rubric, checks, evidence, strict=True)
        ]

    def _customer_artifacts(self, incident: Incident) -> list[dict[str, object]]:
        artifacts = self._pack(incident).artifacts
        result = []
        for key, content in (
            (
                "crm_thread",
                (
                    "Renewal sponsor reports a production provisioning gap and requests a "
                    "response before the executive review."
                ),
            ),
            (
                "entitlement",
                (
                    "Enterprise plan includes the affected provisioning capability and "
                    "service-credit remedy."
                ),
            ),
            (
                "call",
                "Customer executive: ownership, remedy, and a written checkpoint are "
                "required today.",
            ),
        ):
            result.append(
                {
                    **artifacts[key],
                    "timestamp": "T+0",
                    "visibility": "account-team",
                    "version": "v1",
                    "content": content,
                }
            )
        if incident.investigated:
            result.append(
                {
                    **artifacts["finding"],
                    "timestamp": f"T+{incident.time}",
                    "visibility": "account-team",
                    "version": "v1",
                    "content": (
                        "Provisioning entitlement was not applied during the account migration."
                    ),
                }
            )
        if incident.rollback:
            result.append(
                {
                    **artifacts["remedy"],
                    "timestamp": f"T+{incident.time}",
                    "visibility": "account-team",
                    "version": "v1",
                    "content": "Provisioning fix and documented service credit are approved.",
                }
            )
        return result

    def _customer_debrief(self, incident: Incident, completed: bool) -> list[dict[str, object]]:
        rubric = self._pack(incident).rubric
        checks = (
            (
                incident.declared,
                "Escalation ownership was acknowledged before the executive follow-up.",
            ),
            (
                incident.investigated,
                "CRM, entitlement, and call evidence established the provisioning cause.",
            ),
            (incident.rollback, "The entitled provisioning fix and service remedy were approved."),
            (completed, "The customer received a confirmed remedy and follow-up checkpoint."),
        )
        evidence = (
            "scenario.action.acknowledge_escalation",
            "finding-provisioning-gap",
            "approved-service-remedy",
            "scenario.action.send_resolution",
        )
        return [
            {
                "criterion": item["criterion"],
                "detail": detail,
                "score": int(ok),
                "evidence": self._links(self._event_index(incident, evidence_item), evidence_item),
            }
            for item, (ok, detail), evidence_item in zip(rubric, checks, evidence, strict=True)
        ]
