"""Recorded-HTTP routing layer for the twin: GET/HEAD replay and mutation capture.

Nothing in this module ever performs a live network call. GET/HEAD requests
are answered from the merged snapshot (recorded fixture reads, overridden by
anything freshly captured immediately before rehearsal); mutating requests
are captured --- never forwarded --- and matched against a compatible
observed response template. Anything that does not match (an unseen host, an
unmatched GET/HEAD request, or a mutation with no compatible template)
aborts the rehearsal. There is no LLM-improvised fallback: a response is
never invented for anything unmatched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from apprentice.twin.snapshot import MergedSnapshot, host_of, request_key

__all__ = [
    "CapturedMutation",
    "ResponseProvenance",
    "ResponseRouter",
    "RoutedResponse",
    "TwinAbortError",
]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAFE_METHODS = frozenset({"GET", "HEAD"})


class ResponseProvenance(StrEnum):
    OBSERVED = "observed"
    CURRENT_SNAPSHOT = "current_snapshot"
    IMPROVISED = "improvised"


@dataclass(frozen=True)
class RoutedResponse:
    status: int
    headers: dict[str, str]
    body: Any
    provenance: ResponseProvenance


@dataclass(frozen=True)
class CapturedMutation:
    """The exact outbound mutation the twin captured instead of forwarding."""

    method: str
    url: str
    headers: dict[str, str]
    body: Any
    response: RoutedResponse


class TwinAbortError(RuntimeError):
    """Raised whenever the twin must fail closed rather than guess."""

    def __init__(self, reason: str, *, method: str, url: str) -> None:
        super().__init__(f"{reason}: {method} {url}")
        self.reason = reason
        self.method = method
        self.url = url


class ResponseRouter:
    """Serves GET/HEAD from the merged snapshot; captures mutations for template matching."""

    def __init__(
        self,
        *,
        merged: MergedSnapshot,
        response_templates: Sequence[Mapping[str, Any]],
        allowed_hosts: frozenset[str],
    ) -> None:
        self._merged = merged
        self._templates = list(response_templates)
        self._allowed_hosts = allowed_hosts
        self.captured_mutations: list[CapturedMutation] = []
        self.visited_hosts: set[str] = set()

    def get(self, url: str, *, method: str = "GET") -> RoutedResponse:
        """Serve one GET/HEAD request from the merged snapshot, or abort."""

        normalized_method = method.upper()
        if normalized_method not in _SAFE_METHODS:
            raise ValueError(f"get() only serves GET/HEAD, not {method}")
        key = request_key(normalized_method, url)
        host = key[1]
        self.visited_hosts.add(host)
        if host not in self._allowed_hosts:
            raise TwinAbortError("unseen_host", method=normalized_method, url=url)
        entry = self._merged.entries.get(key)
        if entry is None:
            raise TwinAbortError("unmatched_request", method=normalized_method, url=url)
        return RoutedResponse(
            status=entry["status"],
            headers=dict(entry["headers"]),
            body=entry["body"],
            provenance=ResponseProvenance(entry["provenance"]),
        )

    def capture_mutation(
        self, *, method: str, url: str, headers: Mapping[str, str], body: Any
    ) -> RoutedResponse:
        """Capture a mutating request --- it is never forwarded --- and answer from a template."""

        normalized_method = method.upper()
        if normalized_method not in _MUTATING_METHODS:
            raise ValueError(f"capture_mutation only accepts mutating methods, not {method}")
        host = host_of(url)
        self.visited_hosts.add(host)
        if host not in self._allowed_hosts:
            raise TwinAbortError("unseen_host", method=normalized_method, url=url)
        template = self._compatible_template(normalized_method, url)
        if template is None:
            raise TwinAbortError("unknown_mutation_template", method=normalized_method, url=url)
        response = RoutedResponse(
            status=template["response"]["status"],
            headers=dict(template["response"].get("headers") or {}),
            body=template["response"].get("body"),
            provenance=ResponseProvenance.OBSERVED,
        )
        self.captured_mutations.append(
            CapturedMutation(
                method=normalized_method,
                url=url,
                headers=dict(headers),
                body=body,
                response=response,
            )
        )
        return response

    def _compatible_template(self, method: str, url: str) -> Mapping[str, Any] | None:
        """A template is compatible when its method and normalized URL match exactly."""

        key = request_key(method, url)
        for template in self._templates:
            request = template["request"]
            if str(request["method"]).upper() != method:
                continue
            if request_key(method, request["url"]) == key:
                return template
        return None
