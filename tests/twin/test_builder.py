from __future__ import annotations

from pathlib import Path

import pytest

from apprentice.recorder.artifacts import load_artifact
from apprentice.twin.builder import build_twin
from apprentice.twin.responses import ResponseProvenance, ResponseRouter, TwinAbortError
from apprentice.twin.snapshot import SnapshotEntry, merge_over_fixture

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"

_FIXTURE_ENTRIES = [
    {
        "method": "GET",
        "url": "http://127.0.0.1/expense",
        "response": {"status": 200, "headers": {}, "body": "<html>form</html>"},
    },
]

_TEMPLATES = [
    {
        "request": {
            "method": "POST",
            "url": "http://127.0.0.1/expense",
            "headers": {"content-type": "multipart/form-data"},
            "body": {"kind": "multipart", "fields": {"amount": "42.50"}, "files": []},
        },
        "response": {
            "status": 201,
            "headers": {"content-type": "text/html"},
            "body": "<html>submitted</html>",
        },
        "source": "observed",
    }
]


def _router(*, allowed_hosts=frozenset({"127.0.0.1"})) -> ResponseRouter:
    merged = merge_over_fixture(_FIXTURE_ENTRIES, ())
    return ResponseRouter(merged=merged, response_templates=_TEMPLATES, allowed_hosts=allowed_hosts)


# --- ResponseRouter: exact GET/HEAD matching, mutation capture, fail-closed aborts ---


def test_get_serves_the_merged_entry_with_its_provenance() -> None:
    router = _router()

    response = router.get("http://127.0.0.1/expense")

    assert response.status == 200
    assert response.body == "<html>form</html>"
    assert response.provenance is ResponseProvenance.OBSERVED
    assert router.visited_hosts == {"127.0.0.1"}


def test_get_to_an_unseen_host_aborts_without_matching_anything() -> None:
    router = _router()

    with pytest.raises(TwinAbortError) as excinfo:
        router.get("http://attacker.example/expense")

    assert excinfo.value.reason == "unseen_host"


def test_get_to_an_allowed_host_with_no_matching_entry_aborts() -> None:
    router = _router()

    with pytest.raises(TwinAbortError) as excinfo:
        router.get("http://127.0.0.1/does-not-exist")

    assert excinfo.value.reason == "unmatched_request"


def test_capture_mutation_never_forwards_and_returns_the_compatible_template() -> None:
    router = _router()

    response = router.capture_mutation(
        method="POST",
        url="http://127.0.0.1/expense",
        headers={"content-type": "multipart/form-data"},
        body={"kind": "multipart", "fields": {"amount": "87.25"}, "files": []},
    )

    assert response.status == 201
    assert response.provenance is ResponseProvenance.OBSERVED
    assert len(router.captured_mutations) == 1
    captured = router.captured_mutations[0]
    assert captured.method == "POST"
    assert captured.body == {"kind": "multipart", "fields": {"amount": "87.25"}, "files": []}


def test_capture_mutation_to_an_unseen_host_aborts_and_captures_nothing() -> None:
    router = _router()

    with pytest.raises(TwinAbortError) as excinfo:
        router.capture_mutation(
            method="POST",
            url="http://attacker.example/steal",
            headers={},
            body={"kind": "multipart", "fields": {}, "files": []},
        )

    assert excinfo.value.reason == "unseen_host"
    assert router.captured_mutations == []


def test_capture_mutation_with_no_compatible_template_aborts_rather_than_inventing_success() -> (
    None
):
    router = ResponseRouter(
        merged=merge_over_fixture(_FIXTURE_ENTRIES, ()),
        response_templates=(),
        allowed_hosts=frozenset({"127.0.0.1"}),
    )

    with pytest.raises(TwinAbortError) as excinfo:
        router.capture_mutation(
            method="POST",
            url="http://127.0.0.1/expense",
            headers={},
            body={"kind": "multipart", "fields": {"amount": "87.25"}, "files": []},
        )

    assert excinfo.value.reason == "unknown_mutation_template"
    assert router.captured_mutations == []


# --- build_twin: merges a fresh snapshot over a recorded fixture ---


def test_build_twin_defaults_snapshot_urls_to_the_fixtures_own_get_reads() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    calls: list[str] = []

    def fetcher(method: str, url: str) -> SnapshotEntry:
        calls.append(url)
        return SnapshotEntry(
            method=method, url=url, status=200, headers={}, body="<html>live</html>"
        )

    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], fetcher=fetcher)

    assert calls == ["http://127.0.0.1/expense"]
    response = twin.router.get("http://127.0.0.1/expense")
    assert response.body == "<html>live</html>"
    assert response.provenance is ResponseProvenance.CURRENT_SNAPSHOT
    assert twin.observed_hosts == frozenset({"127.0.0.1"})
    assert twin.allowed_hosts == frozenset({"127.0.0.1"})


def test_build_twin_falls_back_to_the_fixture_read_when_snapshot_fetch_is_not_requested() -> None:
    fixture = load_artifact(FIXTURES / "training_1")

    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())

    response = twin.router.get("http://127.0.0.1/expense")
    assert response.provenance is ResponseProvenance.OBSERVED
    assert "<title>File an expense</title>" in response.body


def test_build_twin_observed_hosts_include_the_mutation_templates_host() -> None:
    fixture = load_artifact(FIXTURES / "training_1")

    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())

    assert "127.0.0.1" in twin.observed_hosts
    response = twin.router.capture_mutation(
        method="POST",
        url="http://127.0.0.1/expense",
        headers={"content-type": "multipart/form-data"},
        body={
            "kind": "multipart",
            "fields": {"merchant": "Acme Supplies", "amount": "42.50", "justification": ""},
            "files": [
                {
                    "field": "receipt",
                    "filename": "receipt.pdf",
                    "content_type": "application/pdf",
                    "sha256": "78d34c0be3938936c1ea608f35cc9eb85e663b9e3e1954c2e48a18a412580810",
                }
            ],
        },
    )
    assert response.status == 201


def test_build_twin_current_snapshot_reflects_the_changed_site() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    changed = load_artifact(FIXTURES / "changed_site")
    changed_body = changed["http_entries"][0]["response"]["body"]

    def fetcher(method: str, url: str) -> SnapshotEntry:
        return SnapshotEntry(method=method, url=url, status=200, headers={}, body=changed_body)

    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], fetcher=fetcher)

    response = twin.router.get("http://127.0.0.1/expense")
    assert 'name="total_amount"' in response.body
    assert response.provenance is ResponseProvenance.CURRENT_SNAPSHOT


def test_build_twin_rejects_snapshot_host_before_fetching() -> None:
    calls: list[str] = []

    def fetcher(method: str, url: str) -> SnapshotEntry:
        calls.append(url)
        return SnapshotEntry(method=method, url=url, status=200, headers={}, body="unused")

    with pytest.raises(ValueError, match="undeclared host"):
        build_twin(
            {"http_entries": [], "response_templates": []},
            allowed_hosts=["allowed.example"],
            snapshot_urls=["http://blocked.example/internal"],
            fetcher=fetcher,
        )

    assert calls == []
