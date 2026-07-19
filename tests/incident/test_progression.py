from __future__ import annotations

from fastapi.testclient import TestClient

from apprentice.sidecar.app import build_sidecar


def test_completed_incidents_build_evidence_backed_learner_signals(tmp_path) -> None:
    client = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    created = client.post(
        "/api/learners", json={"id": "learner-ada", "display_name": "Ada Lovelace"}
    )
    assert created.status_code == 201

    incident = client.post("/api/incidents", json={"learner_id": "learner-ada"}).json()
    incident_id = incident["id"]
    for kind in ("declare_incident", "investigate", "rollback", "communicate"):
        response = client.post(f"/api/incidents/{incident_id}/actions", json={"kind": kind})
        assert response.status_code == 200
    assert response.json()["outcome"] == "recovered"

    profile = client.get("/api/learners/learner-ada")
    assert profile.status_code == 200
    payload = profile.json()
    assert payload["completed_runs"] == 1
    assert payload["history"][0]["incident_id"] == incident_id
    assert payload["history"][0]["outcome"] == "recovered"
    skills = {signal["skill"]: signal for signal in payload["skills"]}
    assert skills["incident_declaration"]["score"] == 1.0
    assert skills["evidence_based_diagnosis"]["score"] == 1.0
    assert skills["recovery_validation"]["evidence"][0]["evidence"]

    history = client.get("/api/learners/learner-ada/history")
    assert history.status_code == 200
    assert history.json()["history"] == payload["history"]

    restarted = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    assert restarted.get("/api/learners/learner-ada").json()["history"] == payload["history"]


def test_unfinished_runs_do_not_inflate_progression_and_unknown_learner_is_rejected(
    tmp_path,
) -> None:
    client = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    client.post("/api/learners", json={"id": "learner-grace", "display_name": "Grace Hopper"})

    unknown = client.post("/api/incidents", json={"learner_id": "missing"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "unknown_learner"

    incident_id = client.post("/api/incidents", json={"learner_id": "learner-grace"}).json()["id"]
    client.post(f"/api/incidents/{incident_id}/actions", json={"kind": "declare_incident"})
    profile = client.get("/api/learners/learner-grace").json()
    assert profile["completed_runs"] == 0
    assert all(signal["score"] is None for signal in profile["skills"])


def test_instructor_can_compare_selected_learners_with_persisted_evidence_and_replay_links(
    tmp_path,
) -> None:
    client = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    client.post(
        "/api/learners", json={"id": "learner-ada", "display_name": "Ada Lovelace"}
    )
    client.post(
        "/api/learners", json={"id": "learner-grace", "display_name": "Grace Hopper"}
    )

    incident_id = client.post("/api/incidents", json={"learner_id": "learner-ada"}).json()["id"]
    for kind in ("declare_incident", "investigate", "rollback", "communicate"):
        response = client.post(f"/api/incidents/{incident_id}/actions", json={"kind": kind})
        assert response.status_code == 200

    review = client.get(
        "/api/instructor/review",
        params=[("learner_id", "learner-ada"), ("learner_id", "learner-grace")],
    )
    assert review.status_code == 200
    payload = review.json()
    assert payload["selected_learner_ids"] == ["learner-ada", "learner-grace"]
    ada, grace = payload["learners"]
    assert ada["completed_attempts"][0]["incident_id"] == incident_id
    assert ada["completed_attempts"][0]["replay_url"] == f"/api/incidents/{incident_id}/replay"
    assert ada["completed_attempts"][0]["debrief_url"] == f"/api/incidents/{incident_id}/debrief"
    assert ada["completed_attempts"][0]["debrief"]
    assert grace["completed_attempts"] == []
    signals = {item["skill"]: item for item in payload["skill_comparison"]}
    assert signals["incident_declaration"]["learners"][0]["score"] == 1.0
    assert signals["incident_declaration"]["learners"][0]["evidence"]
    assert signals["incident_declaration"]["learners"][1]["score"] is None

    restarted = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    persisted = restarted.get(
        "/api/instructor/review", params=[("learner_id", "learner-ada")]
    ).json()
    assert persisted["learners"][0]["completed_attempts"][0]["incident_id"] == incident_id


def test_instructor_review_rejects_unknown_or_duplicate_selected_learners(tmp_path) -> None:
    client = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    client.post(
        "/api/learners", json={"id": "learner-ada", "display_name": "Ada Lovelace"}
    )

    unknown = client.get("/api/instructor/review", params={"learner_id": "missing"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "unknown_learner"

    duplicate = client.get(
        "/api/instructor/review",
        params=[("learner_id", "learner-ada"), ("learner_id", "learner-ada")],
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "invalid_review_selection"
