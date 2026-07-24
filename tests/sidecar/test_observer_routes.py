from __future__ import annotations

from fastapi.testclient import TestClient

from apprentice.observability import LocalTraceStore
from apprentice.sidecar.app import build_app

TOKEN = "owner-test-token-with-enough-entropy"


def _environment(tmp_path) -> dict[str, str]:
    return {
        "APPRENTICE_OBSERVER_ENABLED": "true",
        "APPRENTICE_OBSERVER_TOKEN": TOKEN,
        "APPRENTICE_TRACE_PATH": str(tmp_path / "traces.jsonl"),
    }


def _client(tmp_path, *, host: str = "127.0.0.1") -> tuple[TestClient, LocalTraceStore]:
    store = LocalTraceStore(tmp_path / "traces.jsonl")
    app = build_app(
        db_path=tmp_path / "apprentice.db",
        trace_store=store,
        environ=_environment(tmp_path),
    )
    return TestClient(app, client=(host, 50000)), store


def test_observer_is_absent_when_disabled(tmp_path) -> None:
    app = build_app(db_path=tmp_path / "apprentice.db", environ={})
    client = TestClient(app, client=("127.0.0.1", 50000))

    assert client.get("/observer").status_code == 404
    assert client.get("/api/observer/traces").status_code == 404


def test_observer_requires_token_when_enabled(tmp_path) -> None:
    try:
        build_app(
            db_path=tmp_path / "apprentice.db",
            environ={"APPRENTICE_OBSERVER_ENABLED": "true"},
        )
    except ValueError as error:
        assert "APPRENTICE_OBSERVER_TOKEN" in str(error)
    else:
        raise AssertionError("Expected missing observer token to fail startup")


def test_observer_session_sets_strict_http_only_cookie_without_url_token(tmp_path) -> None:
    client, _store = _client(tmp_path)

    bootstrap = client.get("/observer")
    response = client.post("/api/observer/session", json={"token": TOKEN})

    assert bootstrap.status_code == 200
    assert "Owner token" in bootstrap.text
    assert "location.hash" in bootstrap.text
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    dashboard = client.get("/observer")
    assert dashboard.status_code == 200
    assert "How this situation was formed" in dashboard.text
    assert "not hidden chain-of-thought" in dashboard.text


def test_observer_rejects_non_loopback_clients_even_with_token(tmp_path) -> None:
    client, _store = _client(tmp_path, host="192.0.2.8")

    assert client.get("/observer", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_observer_api_accepts_bearer_and_returns_real_trace_data(tmp_path) -> None:
    client, store = _client(tmp_path)
    store.append(
        {
            "id": "event-1",
            "parent_id": None,
            "session_id": "session-1",
            "kind": "operation",
            "name": "practice.start",
            "started_at": 100.0,
            "ended_at": 101.0,
            "duration_ms": 1000.0,
            "status": "success",
            "input": {"content_recorded": False},
            "metadata": {"difficulty_level": 4},
        }
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}

    listed = client.get("/api/observer/traces", headers=headers)
    detailed = client.get("/api/observer/traces/session-1", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["traces"][0]["session_id"] == "session-1"
    assert detailed.status_code == 200
    assert detailed.json()["events"][0]["name"] == "practice.start"
    assert client.get("/api/observer/traces").status_code == 404


def test_observer_static_dashboard_has_working_controls(tmp_path) -> None:
    client, _store = _client(tmp_path)
    client.post("/api/observer/session", json={"token": TOKEN})

    script = client.get("/static/observer.js")

    assert script.status_code == 200
    assert 'fetch("/api/observer/traces")' in script.text
    assert "navigator.clipboard.writeText" in script.text
    assert "data-filter" in script.text
    assert "data-tab" in script.text
