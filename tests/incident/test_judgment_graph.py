from __future__ import annotations

from fastapi.testclient import TestClient

from apprentice.sidecar.app import build_sidecar


def _completed_run(client: TestClient, learner_id: str) -> str:
    incident_id = client.post("/api/incidents", json={"learner_id": learner_id}).json()["id"]
    for kind in ("declare_incident", "investigate", "rollback", "communicate"):
        assert (
            client.post(f"/api/incidents/{incident_id}/actions", json={"kind": kind}).status_code
            == 200
        )
    return incident_id


def test_incident_judgment_graph_links_canonical_decisions_evidence_tradeoffs_outcomes_and_skills(
    tmp_path,
) -> None:
    client = TestClient(build_sidecar(tmp_path / "apprentice.db"))
    assert (
        client.post("/api/learners", json={"id": "ada", "display_name": "Ada"}).status_code == 201
    )
    incident_id = _completed_run(client, "ada")

    graph = client.get(f"/api/incidents/{incident_id}/judgment-graph")
    assert graph.status_code == 200
    payload = graph.json()
    nodes = {node["id"]: node for node in payload["nodes"]}
    prefix = f"incident:{incident_id}"
    assert nodes[f"{prefix}:scenario"]["scenario_id"] == "checkout-worker-lease-leak"
    assert nodes[f"{prefix}:event:1"]["type"] == "decision"
    assert nodes[f"{prefix}:tradeoff:4"]["metric_delta"]["queue_depth"] < 0
    assert nodes[f"{prefix}:outcome"]["outcome"] == "recovered"
    assert nodes[f"{prefix}:skill:evidence_based_diagnosis"]["score"] == 1
    assert {(edge["from"], edge["to"], edge["relationship"]) for edge in payload["edges"]} >= {
        (f"{prefix}:event:2", f"{prefix}:artifact:finding-worker-concurrency", "uses_evidence"),
        (f"{prefix}:tradeoff:4", f"{prefix}:outcome", "contributes_to"),
        (f"{prefix}:outcome", f"{prefix}:skill:service_mitigation", "assesses"),
    }


def test_judgment_graph_is_reconstructed_from_persisted_run_and_debrief_after_restart(
    tmp_path,
) -> None:
    db_path = tmp_path / "apprentice.db"
    client = TestClient(build_sidecar(db_path))
    client.post("/api/learners", json={"id": "ada", "display_name": "Ada"})
    incident_id = _completed_run(client, "ada")
    before = client.get(f"/api/incidents/{incident_id}/judgment-graph").json()

    restarted = TestClient(build_sidecar(db_path))
    assert restarted.get(f"/api/incidents/{incident_id}/judgment-graph").json() == before
    portfolio = restarted.get("/api/learners/ada/judgment-graph")
    assert portfolio.status_code == 200
    assert portfolio.json()["runs"][0] == before
    signals = {signal["skill"]: signal for signal in portfolio.json()["skills"]}
    assert signals["recovery_validation"]["score"] == 1.0
