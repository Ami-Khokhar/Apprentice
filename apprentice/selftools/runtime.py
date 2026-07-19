"""Fail-closed runtime for declarative read-only HTTP self-tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import ValidationError

from apprentice.selftools.models import ExpensePolicy, ReadOnlyHttpManifest


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes | str | Mapping[str, Any]
    url: str


class HttpTransport(Protocol):
    def request(self, *, method: str, url: str, timeout: float) -> HttpResponse: ...


class ToolExecutionError(RuntimeError):
    """A policy lookup was denied or returned an invalid response."""


@dataclass(frozen=True)
class SelfTestResult:
    passed: bool
    detail: str


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def lookup_expense_policy(
    manifest: ReadOnlyHttpManifest,
    *,
    merchant: str,
    amount: float,
    transport: HttpTransport,
) -> ExpensePolicy:
    """Execute the single allowed GET and strictly validate its typed response."""

    if not merchant.strip() or amount < 0:
        raise ToolExecutionError("merchant must be non-empty and amount must be non-negative")
    if manifest.method != "GET":  # defensive even though the model makes this impossible
        raise ToolExecutionError("only GET tools are supported")
    base = str(manifest.base_url).rstrip("/")
    url = f"{base}{manifest.path}?{urlencode({'merchant': merchant, 'amount': str(amount)})}"
    if _origin(url) != _origin(base):
        raise ToolExecutionError("request host is not declared by the manifest")
    try:
        response = transport.request(method="GET", url=url, timeout=manifest.timeout_seconds)
    except Exception as exc:
        raise ToolExecutionError("policy service request failed") from exc
    if _origin(response.url) != _origin(base):
        raise ToolExecutionError("policy service redirected to an undeclared host")
    if response.status != 200:
        raise ToolExecutionError(f"policy service returned HTTP {response.status}")
    try:
        payload = response.body
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        return ExpensePolicy.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise ToolExecutionError("policy service returned an invalid schema") from exc


def selftest_expense_policy_tool(
    manifest: ReadOnlyHttpManifest, transport: HttpTransport
) -> SelfTestResult:
    """Exercise the real runtime through an injected twin transport, never implicit network I/O."""

    try:
        policy = lookup_expense_policy(
            manifest, merchant="Self Test Merchant", amount=1250, transport=transport
        )
    except ToolExecutionError as exc:
        return SelfTestResult(False, str(exc))
    return SelfTestResult(True, f"validated policy {policy.gl_code}/{policy.cost_center}")
