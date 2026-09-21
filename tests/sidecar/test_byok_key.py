"""Hosted practice runs on a learner key held only in the open page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apprentice.database import SQLiteDatabase
from apprentice.sidecar.app import build_app

HOSTED = {"APPRENTICE_MULTI_USER": "true", "APPRENTICE_PRACTICE_MODEL": "test-model"}
KEY = "sk-test-0123456789abcdefghij"


def _client(tmp_path, environ: dict[str, str]) -> TestClient:
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    return TestClient(build_app(database=database, environ=dict(environ)))


def test_hosted_entry_has_a_masked_in_memory_key_control(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)

    page = client.get("/").text

    assert "<summary>API key</summary>" in page
    assert 'type="password"' in page
    assert "data-api-key-form" in page
    assert "data-api-key-required" in page
    assert "Held only for this page." in page
    assert "never saved to a cookie, database, file, or log" in page


def test_the_key_is_never_rendered_back_into_the_page(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)

    page = client.get("/").text

    assert KEY not in page

    assert "apprentice_api_key" not in client.cookies


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
