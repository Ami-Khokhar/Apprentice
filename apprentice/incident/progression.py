"""Persistent, evidence-backed learner progression for Incident Command."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apprentice.ledger.db import SQLiteDatabase


class LearnerNotFoundError(KeyError):
    pass


class IncidentAlreadyAssignedError(ValueError):
    pass


class InvalidReviewSelectionError(ValueError):
    pass


_SKILLS = {
    "Timing": "incident_declaration",
    "Evidence": "evidence_based_diagnosis",
    "Mitigation": "service_mitigation",
    "Communication": "stakeholder_communication",
    "Validation": "recovery_validation",
}


class LearnerProgression:
    """Store learner attempts and derive skill signals from canonical debriefs."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def create_learner(self, learner_id: str, display_name: str) -> dict[str, object]:
        learner_id = learner_id.strip()
        display_name = display_name.strip()
        if not learner_id:
            raise ValueError("Learner id must not be blank")
        if not display_name:
            raise ValueError("Learner display name must not be blank")
        now = time.time()
        with self._database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO incident_learners (id, display_name, created_at) VALUES (?, ?, ?)",
                (learner_id, display_name, now),
            )
        return self.profile(learner_id)

    def assign_run(self, learner_id: str, incident_id: str) -> None:
        self._ensure_learner(learner_id)
        with self._database.transaction(write=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO incident_learner_runs (incident_id, learner_id) VALUES (?, ?)",
                    (incident_id, learner_id),
                )
            except sqlite3.IntegrityError as error:
                if "UNIQUE constraint failed: incident_learner_runs.incident_id" in str(error):
                    raise IncidentAlreadyAssignedError(
                        f"Incident {incident_id} is already assigned to a learner"
                    ) from error
                raise

    def record_outcome(self, snapshot: dict[str, object]) -> None:
        """Persist the final rubric once; later reads never recompute old evidence."""
        outcome = snapshot.get("outcome")
        if outcome not in {"recovered", "terminal_escalation"}:
            return
        debrief = snapshot.get("debrief")
        incident_id = snapshot.get("id")
        if not isinstance(incident_id, str) or not isinstance(debrief, list):
            raise ValueError("Completed incident snapshot is missing its canonical debrief")
        with self._database.transaction(write=True) as connection:
            updated = connection.execute(
                "UPDATE incident_learner_runs "
                "SET completed_at = COALESCE(completed_at, ?), "
                "outcome = COALESCE(outcome, ?), debrief_json = COALESCE(debrief_json, ?) "
                "WHERE incident_id = ?",
                (time.time(), outcome, json.dumps(debrief, sort_keys=True), incident_id),
            )
        if updated.rowcount == 0:
            return

    def profile(self, learner_id: str) -> dict[str, object]:
        learner, history = self._learner_and_history(learner_id)
        return {
            "id": learner["id"],
            "display_name": learner["display_name"],
            "created_at": learner["created_at"],
            "completed_runs": len(history),
            "history": history,
            "skills": self._skills(history),
        }

    def compare(self, learner_ids: list[str]) -> dict[str, object]:
        """Return completed attempts and evidence-backed signals for selected learners.

        This intentionally accepts an explicit selection rather than introducing
        mutable team membership before the product has team administration.
        Every attempt and signal comes from the durable completion debrief, and
        replay URLs point to the immutable incident event history.
        """
        if not learner_ids:
            raise InvalidReviewSelectionError("Select at least one learner to review")
        if len(set(learner_ids)) != len(learner_ids):
            raise InvalidReviewSelectionError("Each selected learner must appear only once")

        learners: list[dict[str, object]] = []
        for learner_id in learner_ids:
            learner, history = self._learner_and_history(learner_id)
            attempts = [
                {
                    **run,
                    "replay_url": f"/api/incidents/{run['incident_id']}/replay",
                    "debrief_url": f"/api/incidents/{run['incident_id']}/debrief",
                }
                for run in history
            ]
            learners.append(
                {
                    "id": learner["id"],
                    "display_name": learner["display_name"],
                    "completed_runs": len(history),
                    "completed_attempts": attempts,
                    "skills": self._skills(history),
                }
            )

        return {
            "selected_learner_ids": learner_ids,
            "learners": learners,
            "skill_comparison": [
                {
                    "skill": skill,
                    "learners": [
                        {
                            "learner_id": learner["id"],
                            "attempts": signal["attempts"],
                            "demonstrated": signal["demonstrated"],
                            "score": signal["score"],
                            "evidence": signal["evidence"],
                        }
                        for learner in learners
                        for signal in learner["skills"]
                        if signal["skill"] == skill
                    ],
                }
                for skill in _SKILLS.values()
            ],
        }

    def _learner_and_history(self, learner_id: str) -> tuple[sqlite3.Row, list[dict[str, object]]]:
        with self._database.transaction() as connection:
            learner = connection.execute(
                "SELECT id, display_name, created_at FROM incident_learners WHERE id = ?",
                (learner_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT incident_id, completed_at, outcome, debrief_json "
                "FROM incident_learner_runs WHERE learner_id = ? AND completed_at IS NOT NULL "
                "ORDER BY completed_at DESC, incident_id DESC",
                (learner_id,),
            ).fetchall()
        if learner is None:
            raise LearnerNotFoundError(f"Unknown learner: {learner_id}")
        history = [
            {
                "incident_id": row["incident_id"],
                "completed_at": row["completed_at"],
                "outcome": row["outcome"],
                "debrief": json.loads(row["debrief_json"]),
            }
            for row in rows
        ]
        return learner, history

    def _ensure_learner(self, learner_id: str) -> None:
        with self._database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM incident_learners WHERE id = ?", (learner_id,)
            ).fetchone()
        if exists is None:
            raise LearnerNotFoundError(f"Unknown learner: {learner_id}")

    @staticmethod
    def _skills(history: list[dict[str, object]]) -> list[dict[str, object]]:
        signals = {
            skill: {"attempts": 0, "demonstrated": 0, "evidence": []}
            for skill in _SKILLS.values()
        }
        for run in history:
            debrief = run["debrief"]
            assert isinstance(debrief, list)
            for item in debrief:
                if not isinstance(item, dict):
                    continue
                skill = _SKILLS.get(item.get("criterion"))
                if skill is None:
                    continue
                signal = signals[skill]
                score = int(item.get("score", 0))
                signal["attempts"] += 1
                signal["demonstrated"] += score
                signal["evidence"].append(
                    {
                        "incident_id": run["incident_id"],
                        "criterion": item["criterion"],
                        "score": score,
                        "evidence": item.get("evidence", []),
                    }
                )
        return [
            {
                "skill": skill,
                "attempts": signal["attempts"],
                "demonstrated": signal["demonstrated"],
                "score": round(signal["demonstrated"] / signal["attempts"], 2)
                if signal["attempts"]
                else None,
                "evidence": signal["evidence"],
            }
            for skill, signal in signals.items()
        ]
