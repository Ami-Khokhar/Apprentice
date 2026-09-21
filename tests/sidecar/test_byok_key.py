"""Hosted practice runs on the learner's own key, held only for their browser session."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apprentice.database import SQLiteDatabase
from apprentice.sidecar.app import API_KEY_COOKIE, build_app

HOSTED = {"APPRENTICE_MULTI_USER": "true", "APPRENTICE_PRACTICE_MODEL": "test-model"}
KEY = "sk-test-0123456789abcdefghij"


def _client(tmp_path, environ: dict[str, str]) -> TestClient:
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    return TestClient(build_app(database=database, environ=dict(environ)))


def test_a_supplied_key_is_kept_in_a_session_cookie(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)

    stored = client.post("/key", data={"api_key": KEY}, follow_redirects=False)
    header = next(
        value
        for value in stored.headers.get_list("set-cookie")
        if value.startswith(f"{API_KEY_COOKIE}=")
    )

    assert stored.status_code == 303
    assert "HttpOnly" in header
    assert "Max-Age" not in header, "the key must not outlive the browser session"
    assert client.cookies[API_KEY_COOKIE] == KEY


def test_the_key_is_never_rendered_back_into_the_page(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)
    client.post("/key", data={"api_key": KEY})

    page = client.get("/").text

    assert KEY not in page
    assert "Forget my key" in page


def test_a_malformed_key_is_rejected_without_being_stored(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)

    rejected = client.post("/key", data={"api_key": "nope"}, follow_redirects=False)

    assert rejected.headers["location"] == "/?key=rejected"
    assert API_KEY_COOKIE not in client.cookies
    assert "did not look like an API key" in client.get("/?key=rejected").text


def test_a_learner_can_forget_their_key(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)
    client.post("/key", data={"api_key": KEY})

    client.post("/key/forget")

    assert client.cookies.get(API_KEY_COOKIE) in (None, "")
    assert "Your OpenAI API key" in client.get("/").text


def test_the_local_install_has_no_key_routes(tmp_path) -> None:
    """A local install authenticates through the Codex CLI, so it asks for no key."""
    client = _client(tmp_path, {})

    assert client.post("/key", data={"api_key": KEY}).status_code == 404
    assert "Your OpenAI API key" not in client.get("/").text


def test_the_database_location_can_be_set_for_hosting(tmp_path) -> None:
    """A container needs the database on a writable path it chooses."""
    target = tmp_path / "state" / "apprentice.db"
    build_app(environ={**HOSTED, "APPRENTICE_DB_PATH": str(target)})

    assert target.exists()
