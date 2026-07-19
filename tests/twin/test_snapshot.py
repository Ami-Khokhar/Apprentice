from __future__ import annotations

from apprentice.twin.snapshot import (
    SnapshotEntry,
    capture_snapshot,
    host_of,
    merge_over_fixture,
    request_key,
)

_FIXTURE_ENTRIES = [
    {
        "method": "GET",
        "url": "http://127.0.0.1/expense",
        "response": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": "<html>original</html>",
        },
    },
    {
        "method": "POST",
        "url": "http://127.0.0.1/expense",
        "response": {
            "status": 201,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": "<html>submitted</html>",
        },
    },
]


def test_request_key_matches_exact_method_host_path_and_query_regardless_of_order() -> None:
    first = request_key("get", "http://127.0.0.1/expense?b=2&a=1")
    second = request_key("GET", "http://127.0.0.1/expense?a=1&b=2")

    assert first == second


def test_request_key_distinguishes_different_hosts_and_paths() -> None:
    assert request_key("GET", "http://127.0.0.1/expense") != request_key(
        "GET", "http://attacker.example/expense"
    )
    assert request_key("GET", "http://127.0.0.1/expense") != request_key(
        "GET", "http://127.0.0.1/other"
    )


def test_host_of_normalizes_case() -> None:
    assert host_of("http://127.0.0.1/expense") == "127.0.0.1"
    assert host_of("HTTP://Example.COM/x") == "example.com"


def test_capture_snapshot_uses_injected_fetcher_and_never_touches_real_network() -> None:
    calls: list[tuple[str, str]] = []

    def fake_fetcher(method: str, url: str) -> SnapshotEntry:
        calls.append((method, url))
        return SnapshotEntry(
            method=method, url=url, status=200, headers={}, body="<html>live</html>"
        )

    entries = capture_snapshot(["http://127.0.0.1/expense"], fetcher=fake_fetcher)

    assert calls == [("GET", "http://127.0.0.1/expense")]
    assert entries == (
        SnapshotEntry(
            method="GET",
            url="http://127.0.0.1/expense",
            status=200,
            headers={},
            body="<html>live</html>",
        ),
    )


def test_merge_over_fixture_prefers_current_snapshot_for_a_matching_key() -> None:
    snapshot = (
        SnapshotEntry(
            method="GET",
            url="http://127.0.0.1/expense",
            status=200,
            headers={"content-type": "text/html"},
            body="<html>current</html>",
        ),
    )

    merged = merge_over_fixture(_FIXTURE_ENTRIES, snapshot)

    key = request_key("GET", "http://127.0.0.1/expense")
    assert merged.entries[key]["body"] == "<html>current</html>"
    assert merged.entries[key]["provenance"] == "current_snapshot"
    assert merged.observed_hosts == frozenset({"127.0.0.1"})
    assert "http://127.0.0.1/expense" in merged.observed_urls


def test_merge_over_fixture_keeps_fixture_reads_that_were_not_resnapshotted() -> None:
    merged = merge_over_fixture(_FIXTURE_ENTRIES, ())

    key = request_key("GET", "http://127.0.0.1/expense")
    assert merged.entries[key]["body"] == "<html>original</html>"
    assert merged.entries[key]["provenance"] == "observed"


def test_merge_over_fixture_ignores_mutating_fixture_entries() -> None:
    merged = merge_over_fixture(_FIXTURE_ENTRIES, ())

    post_key = request_key("POST", "http://127.0.0.1/expense")
    assert post_key not in merged.entries
