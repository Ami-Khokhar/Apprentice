"""Inspectable judgment graphs derived from durable Incident Command evidence.

This is deliberately a projection, not another source of truth or a graph
database.  Rebuilding a graph always reads the persisted incident event
snapshots and the completed debrief (when a learner has one), so a replay and
its judgment evidence cannot diverge after a process restart.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .runtime import IncidentRuntime

if TYPE_CHECKING:
    from apprentice.ledger.db import SQLiteDatabase


_SKILLS = {
    "Timing": "incident_declaration",
    "Evidence": "evidence_based_diagnosis",
    "Mitigation": "service_mitigation",
    "Communication": "stakeholder_communication",
    "Validation": "recovery_validation",
}


class JudgmentGraph:
    """Project canonical runs into scenario → judgment → outcome evidence."""

    def __init__(self, database: SQLiteDatabase, runtime: IncidentRuntime) -> None:
        self._database = database
        self._runtime = runtime

    def incident(self, incident_id: str) -> dict[str, object]:
        snapshot = self._runtime.snapshot(incident_id)
        debrief = self._persisted_debrief(incident_id) or snapshot["debrief"]
        assert isinstance(debrief, list)
        return self._project(snapshot, debrief)

    def learner(self, learner_id: str) -> dict[str, object]:
        """Return an evidence-backed portfolio across completed learner runs."""
        with self._database.transaction() as connection:
            learner = connection.execute(
                "SELECT id, display_name FROM incident_learners WHERE id = ?", (learner_id,)
            ).fetchone()
            rows = connection.execute(
                "SELECT incident_id FROM incident_learner_runs "
                "WHERE learner_id = ? AND completed_at IS NOT NULL "
                "ORDER BY completed_at ASC, incident_id ASC",
                (learner_id,),
            ).fetchall()
        if learner is None:
            # Keep the public API's existing error semantics.
            from .progression import LearnerNotFoundError

            raise LearnerNotFoundError(f"Unknown learner: {learner_id}")

        runs = [self.incident(str(row["incident_id"])) for row in rows]
        signals: dict[str, dict[str, object]] = {
            skill: {"attempts": 0, "demonstrated": 0, "evidence": []} for skill in _SKILLS.values()
        }
        for run in runs:
            for node in run["nodes"]:
                if node["type"] != "skill_signal":
                    continue
                signal = signals[str(node["skill"])]
                signal["attempts"] = int(signal["attempts"]) + 1
                signal["demonstrated"] = int(signal["demonstrated"]) + int(node["score"])
                signal["evidence"].append(
                    {
                        "incident_id": run["incident_id"],
                        "criterion": node["criterion"],
                        "score": node["score"],
                        "node_id": node["id"],
                    }
                )
        return {
            "learner": {"id": learner["id"], "display_name": learner["display_name"]},
            "runs": runs,
            "skills": [
                {
                    "skill": skill,
                    "attempts": value["attempts"],
                    "demonstrated": value["demonstrated"],
                    "score": round(int(value["demonstrated"]) / int(value["attempts"]), 2)
                    if value["attempts"]
                    else None,
                    "evidence": value["evidence"],
                }
                for skill, value in signals.items()
            ],
        }

    def _persisted_debrief(self, incident_id: str) -> list[dict[str, object]] | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT debrief_json FROM incident_learner_runs WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        if row is None or row["debrief_json"] is None:
            return None
        value = json.loads(row["debrief_json"])
        if not isinstance(value, list):
            raise ValueError(f"Incident {incident_id} has an invalid persisted debrief")
        return value

    @staticmethod
    def _project(
        snapshot: dict[str, object], debrief: list[dict[str, object]]
    ) -> dict[str, object]:
        incident_id = str(snapshot["id"])
        prefix = f"incident:{incident_id}"
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, str]] = []

        def node(node_id: str, type: str, **data: object) -> None:
            nodes.append({"id": node_id, "type": type, **data})

        def edge(source: str, target: str, relationship: str) -> None:
            edges.append({"from": source, "to": target, "relationship": relationship})

        scenario_id = f"{prefix}:scenario"
        node(
            scenario_id,
            "scenario",
            scenario_id=snapshot["scenario"],
            scenario_version=snapshot["scenario_version"],
            seed=snapshot["seed"],
        )
        artifact_ids: set[str] = set()
        for artifact in snapshot["artifacts"]:
            artifact_id = str(artifact["id"])
            artifact_ids.add(artifact_id)
            node(
                f"{prefix}:artifact:{artifact_id}",
                "artifact",
                artifact_id=artifact_id,
                title=artifact["title"],
                kind=artifact["kind"],
                source=artifact["source"],
                timestamp=artifact["timestamp"],
            )
            edge(scenario_id, f"{prefix}:artifact:{artifact_id}", "provides_evidence")

        evidence_by_event: dict[int, list[str]] = {}
        for item in debrief:
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                continue
            event_index = next(
                (
                    link["event_index"]
                    for link in evidence
                    if isinstance(link, dict) and "event_index" in link
                ),
                None,
            )
            artifact_id = next(
                (
                    link["artifact_id"]
                    for link in evidence
                    if isinstance(link, dict) and "artifact_id" in link
                ),
                None,
            )
            if isinstance(event_index, int) and isinstance(artifact_id, str):
                evidence_by_event.setdefault(event_index, []).append(artifact_id)

        outcome_id = f"{prefix}:outcome"
        node(
            outcome_id,
            "outcome",
            outcome=snapshot["outcome"],
            completed=snapshot["completed"],
            terminal=snapshot["terminal"],
            metrics=snapshot["metrics"],
        )

        for event in snapshot["events"]:
            event_index = int(event["event_index"])
            event_id = f"{prefix}:event:{event_index}"
            rule = str(event["rule"])
            event_type = (
                "decision" if rule.startswith(("scenario.action.", "actor.delegate.")) else "event"
            )
            node(
                event_id,
                event_type,
                event_index=event_index,
                title=event["title"],
                rule=rule,
                event_type=event["type"],
                time=event["time"],
                detail=event["detail"],
            )
            edge(scenario_id, event_id, "records")
            for artifact_id in sorted(set(evidence_by_event.get(event_index, []))):
                evidence_id = f"{prefix}:artifact:{artifact_id}"
                if artifact_id not in artifact_ids:
                    node(
                        evidence_id,
                        "evidence_reference",
                        artifact_id=artifact_id,
                        provenance="debrief_reference",
                    )
                    artifact_ids.add(artifact_id)
                edge(event_id, evidence_id, "uses_evidence")
            delta = event.get("delta")
            if isinstance(delta, dict) and delta:
                tradeoff_id = f"{prefix}:tradeoff:{event_index}"
                node(
                    tradeoff_id,
                    "tradeoff",
                    event_index=event_index,
                    rule=rule,
                    metric_delta=delta,
                )
                edge(event_id, tradeoff_id, "changes_metrics")
                edge(tradeoff_id, outcome_id, "contributes_to")

        for item in debrief:
            criterion = item.get("criterion")
            skill = _SKILLS.get(criterion)
            if skill is None:
                continue
            skill_id = f"{prefix}:skill:{skill}"
            node(
                skill_id,
                "skill_signal",
                skill=skill,
                criterion=criterion,
                score=int(item.get("score", 0)),
                detail=item.get("detail", ""),
            )
            edge(outcome_id, skill_id, "assesses")

        return {
            "schema_version": 1,
            "incident_id": incident_id,
            "nodes": nodes,
            "edges": edges,
        }
