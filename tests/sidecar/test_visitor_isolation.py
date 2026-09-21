"""Each browser must get its own practice history and Judgment Profile."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apprentice.database import SQLiteDatabase
from apprentice.sidecar.app import VISITOR_COOKIE, build_app

HOSTED = {"APPRENTICE_MULTI_USER": "true", "APPRENTICE_PRACTICE_MODEL": "test-model"}


def _client(tmp_path, environ: dict[str, str]) -> TestClient:
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    return TestClient(build_app(database=database, environ=dict(environ)))


def test_visitors_receive_distinct_owner_cookies(tmp_path) -> None:
    client = _client(tmp_path, HOSTED)

    first = client.get("/profile")
    client.cookies.clear()
    second = client.get("/profile")

    assert first.cookies[VISITOR_COOKIE] != second.cookies[VISITOR_COOKIE]


def test_profile_edits_stay_private_to_each_visitor(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    app = build_app(database=database, environ=dict(HOSTED))

    with TestClient(app) as amber, TestClient(app) as blake:
        amber.post("/profile", data={"display_name": "Amber", "headline": "", "bio": ""})
        blake.post("/profile", data={"display_name": "Blake", "headline": "", "bio": ""})

        assert amber.get("/api/profile").json()["profile"]["display_name"] == "Amber"
        assert blake.get("/api/profile").json()["profile"]["display_name"] == "Blake"


def test_single_user_install_keeps_one_shared_profile(tmp_path) -> None:
    """Without the hosting flag the app stays the local single-user tool."""
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    app = build_app(database=database, environ={})

    with TestClient(app) as first, TestClient(app) as second:
        first.post("/profile", data={"display_name": "Amber", "headline": "", "bio": ""})

        assert second.get("/api/profile").json()["profile"]["display_name"] == "Amber"
        assert VISITOR_COOKIE not in second.cookies
