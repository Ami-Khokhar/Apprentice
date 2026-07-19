"""Assemble one rehearsal twin: a recorded fixture merged with a fresh snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apprentice.twin.responses import ResponseRouter
from apprentice.twin.snapshot import (
    Fetcher,
    capture_snapshot,
    default_fetcher,
    host_of,
    merge_over_fixture,
)

__all__ = ["Twin", "build_twin"]

_SAFE_METHODS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class Twin:
    """One assembled rehearsal environment: routing plus what it has observed."""

    router: ResponseRouter
    allowed_hosts: frozenset[str]
    observed_hosts: frozenset[str]
    observed_urls: frozenset[str]


def _fixture_get_urls(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    """Every distinct GET/HEAD URL the fixture recorded --- the pages a rehearsal needs."""

    seen: dict[str, None] = {}
    for entry in fixture.get("http_entries", []):
        if str(entry["method"]).upper() in _SAFE_METHODS:
            seen.setdefault(entry["url"], None)
    return tuple(seen)


def _template_hosts(response_templates: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(host_of(template["request"]["url"]) for template in response_templates)


def build_twin(
    fixture: Mapping[str, Any],
    *,
    allowed_hosts: Sequence[str],
    fetcher: Fetcher = default_fetcher,
    snapshot_urls: Sequence[str] | None = None,
) -> Twin:
    """Build one rehearsal twin from a recorded fixture and a fresh read-only snapshot.

    ``snapshot_urls`` defaults to every GET/HEAD URL the fixture recorded ---
    the "current target GET pages required by the playbook" the snapshot
    strategy calls for. Tests inject ``fetcher`` so no real network call is
    ever required to exercise the merge.
    """

    allowed = frozenset(allowed_hosts)
    urls = tuple(snapshot_urls) if snapshot_urls is not None else _fixture_get_urls(fixture)
    blocked = [url for url in urls if host_of(url) not in allowed]
    if blocked:
        raise ValueError(f"snapshot URL uses an undeclared host: {blocked[0]}")
    snapshot_entries = capture_snapshot(urls, fetcher=fetcher)
    merged = merge_over_fixture(fixture.get("http_entries", []), snapshot_entries)

    response_templates = fixture.get("response_templates", [])
    router = ResponseRouter(
        merged=merged,
        response_templates=response_templates,
        allowed_hosts=allowed,
    )
    return Twin(
        router=router,
        allowed_hosts=allowed,
        observed_hosts=merged.observed_hosts | _template_hosts(response_templates),
        observed_urls=merged.observed_urls,
    )
