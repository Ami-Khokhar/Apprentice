"""Read-only current-target snapshot capture, merged over recorded fixture reads.

Immediately before rehearsal, the twin performs a read-only GET/HEAD capture
of the current target and merges it *over* the recorded fixture reads: a
freshly captured page wins over a stale recording for the same request, but
anything the fresh capture didn't touch still serves from the fixture. This
is the twin's only real network contact, and it is strictly read-only ---
mutating requests are never snapshotted (see ``apprentice.twin.responses``
for the capture-only handling of POST/PUT/PATCH/DELETE).
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from apprentice.canonical import normalize_hostname

__all__ = [
    "Fetcher",
    "MergedSnapshot",
    "RequestKey",
    "SnapshotEntry",
    "capture_snapshot",
    "default_fetcher",
    "host_of",
    "merge_over_fixture",
    "request_key",
]

_SAFE_METHODS = frozenset({"GET", "HEAD"})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so every contacted origin is explicitly approved."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ValueError(f"refusing to follow snapshot redirect to {newurl}")

RequestKey = tuple[str, str, str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class SnapshotEntry:
    """One read-only capture of a single GET/HEAD request."""

    method: str
    url: str
    status: int
    headers: dict[str, str]
    body: str


Fetcher = Callable[[str, str], SnapshotEntry]


def host_of(url: str) -> str:
    """Return the normalized ``host`` (or ``host:port``) component of a URL."""

    parsed = urlsplit(url)
    if parsed.hostname is None:
        raise ValueError(f"URL has no host: {url}")
    host = normalize_hostname(parsed.hostname)
    return f"{host}:{parsed.port}" if parsed.port is not None else host


def request_key(method: str, url: str) -> RequestKey:
    """The exact (method, host, path, sorted query) identity used for GET/HEAD routing."""

    parsed = urlsplit(url)
    query = tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return method.upper(), host_of(url), parsed.path, query


def default_fetcher(method: str, url: str) -> SnapshotEntry:
    """Perform one real read-only HTTP request; never used for a mutating method."""

    normalized_method = method.upper()
    if normalized_method not in _SAFE_METHODS:
        raise ValueError(f"refusing to snapshot a mutating method: {method}")
    request = urllib.request.Request(url, method=normalized_method)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=10) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset)
        headers = dict(response.headers.items())
        status = response.status
    return SnapshotEntry(
        method=normalized_method, url=url, status=status, headers=headers, body=body
    )


def capture_snapshot(
    urls: Iterable[str],
    *,
    methods: Sequence[str] = ("GET",),
    fetcher: Fetcher = default_fetcher,
) -> tuple[SnapshotEntry, ...]:
    """Read-only capture of the given URLs; the only live contact a rehearsal makes."""

    return tuple(
        fetcher(method, url) for url in urls for method in methods
    )


@dataclass(frozen=True)
class MergedSnapshot:
    """Current-snapshot GET/HEAD reads merged over a recorded fixture's own reads."""

    entries: Mapping[RequestKey, dict[str, Any]]
    observed_hosts: frozenset[str]
    observed_urls: frozenset[str]


def merge_over_fixture(
    fixture_http_entries: Sequence[Mapping[str, Any]],
    snapshot_entries: Sequence[SnapshotEntry],
) -> MergedSnapshot:
    """Merge current-snapshot GET/HEAD reads over a recorded fixture's GET/HEAD reads.

    Only safe (GET/HEAD) fixture entries participate: mutating fixture
    entries are never snapshotted and are served separately from observed
    response templates (see ``apprentice.twin.responses``).
    """

    merged: dict[RequestKey, dict[str, Any]] = {}
    hosts: set[str] = set()
    urls: set[str] = set()

    for entry in fixture_http_entries:
        method = str(entry["method"]).upper()
        if method not in _SAFE_METHODS:
            continue
        url = entry["url"]
        key = request_key(method, url)
        hosts.add(key[1])
        urls.add(url)
        response = entry["response"]
        merged[key] = {
            "status": response["status"],
            "headers": dict(response.get("headers") or {}),
            "body": response.get("body"),
            "provenance": "observed",
        }

    for snapshot_entry in snapshot_entries:
        key = request_key(snapshot_entry.method, snapshot_entry.url)
        hosts.add(key[1])
        urls.add(snapshot_entry.url)
        merged[key] = {
            "status": snapshot_entry.status,
            "headers": dict(snapshot_entry.headers),
            "body": snapshot_entry.body,
            "provenance": "current_snapshot",
        }

    return MergedSnapshot(
        entries=merged,
        observed_hosts=frozenset(hosts),
        observed_urls=frozenset(urls),
    )
