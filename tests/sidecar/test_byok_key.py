"""Hosted practice runs on a learner key held only in the open page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from apprentice.database import SQLiteDatabase
from apprentice.sidecar.app import VISITOR_COOKIE, build_app

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


def _hosted_app(tmp_path):
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    return build_app(
        database=database,
        environ={**HOSTED, "APPRENTICE_PUBLIC_HOST": "apprentice.example"},
    )


def _same_origin_post(client: TestClient):
    return client.post(
        "/profile",
        data={"display_name": "x"},
        headers={
            "Origin": "https://apprentice.example",
            "Sec-Fetch-Site": "same-origin",
            "X-Forwarded-Proto": "https",
        },
        follow_redirects=False,
    )


def test_trusted_proxy_headers_keep_same_origin_posts_and_secure_cookie(tmp_path) -> None:
    app = ProxyHeadersMiddleware(_hosted_app(tmp_path), trusted_hosts="*")
    client = TestClient(app, base_url="http://apprentice.example")

    response = _same_origin_post(client)

    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    visitor = next(cookie for cookie in cookies if cookie.startswith(f"{VISITOR_COOKIE}="))
    assert "Secure" in visitor


def test_untrusted_proxy_headers_reject_same_origin_posts(tmp_path) -> None:
    """Without the proxy middleware the app sees http and rejects the https origin."""
    client = TestClient(_hosted_app(tmp_path), base_url="http://apprentice.example")

    assert _same_origin_post(client).status_code == 403


def test_multi_user_rejects_observer_mode(tmp_path) -> None:
    """Trusted forwarded headers would let a visitor spoof the loopback observer check."""
    with pytest.raises(ValueError, match="APPRENTICE_OBSERVER_ENABLED"):
        build_app(
            database=SQLiteDatabase(tmp_path / "apprentice.db"),
            environ={
                "APPRENTICE_MULTI_USER": "true",
                "APPRENTICE_PRACTICE_MODEL": "m",
                "APPRENTICE_OBSERVER_ENABLED": "true",
                "APPRENTICE_OBSERVER_TOKEN": "t",
            },
        )
