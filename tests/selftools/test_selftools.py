from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from apprentice.selftools import (
    CapabilityRequest,
    HttpResponse,
    ReadOnlyHttpManifest,
    ToolExecutionError,
    draft_expense_policy_tool,
    lookup_expense_policy,
    selftest_expense_policy_tool,
    verify_artifact,
)


def request() -> CapabilityRequest:
    return CapabilityRequest(
        request_id="expense-policy-1",
        name="lookup_expense_policy",
        reason="Accounting fields are absent from the browser.",
        required_outputs=(
            "category",
            "gl_code",
            "cost_center",
            "approval_required",
            "approval_route",
            "justification_threshold",
        ),
    )


class TwinTransport:
    def __init__(self, *, body=None, status=200, response_url=None):
        self.body = body or {
            "category": "office_supplies",
            "gl_code": "6400",
            "cost_center": "OPERATIONS",
            "approval_required": True,
            "approval_route": "manager",
            "justification_threshold": 1000,
        }
        self.status = status
        self.response_url = response_url
        self.calls = []

    def request(self, *, method, url, timeout):
        self.calls.append((method, url, timeout))
        return HttpResponse(self.status, self.body, self.response_url or url)


def manifest() -> ReadOnlyHttpManifest:
    return ReadOnlyHttpManifest(
        name="lookup_expense_policy", base_url="http://policy.internal:8080", timeout_seconds=3
    )


def test_draft_writes_exact_hash_pinned_artifact(tmp_path):
    artifact = draft_expense_policy_tool(
        tmp_path, request(), base_url="http://policy.internal:8080"
    )
    assert {path.name for path in artifact.directory.iterdir()} == {"manifest.toml", "server.py"}
    assert verify_artifact(artifact)
    assert artifact.manifest.method == "GET"


def test_modified_or_extra_artifact_file_fails_verification(tmp_path):
    artifact = draft_expense_policy_tool(
        tmp_path, request(), base_url="http://policy.internal:8080"
    )
    (artifact.directory / "server.py").write_text("changed")
    assert not verify_artifact(artifact)
    (artifact.directory / "unexpected.py").write_text("pass")
    assert not verify_artifact(artifact)


def test_draft_cannot_overwrite_reviewed_request(tmp_path):
    draft_expense_policy_tool(tmp_path, request(), base_url="http://policy.internal:8080")
    with pytest.raises(FileExistsError):
        draft_expense_policy_tool(tmp_path, request(), base_url="http://policy.internal:8080")


def test_manifest_rejects_mutation_method_and_non_origin_url():
    with pytest.raises(ValidationError):
        ReadOnlyHttpManifest(
            name="lookup_expense_policy",
            base_url="http://policy.internal/root",
            method="POST",
            timeout_seconds=3,
        )


def test_lookup_uses_declared_get_timeout_and_validates_response():
    transport = TwinTransport()
    result = lookup_expense_policy(
        manifest(), merchant="Kintsugi & Co", amount=1250, transport=transport
    )
    method, url, timeout = transport.calls[0]
    assert (method, timeout) == ("GET", 3)
    assert urlsplit(url).netloc == "policy.internal:8080"
    assert parse_qs(urlsplit(url).query) == {
        "merchant": ["Kintsugi & Co"],
        "amount": ["1250"],
    }
    assert result.gl_code == "6400"


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (TwinTransport(status=500), "HTTP 500"),
        (TwinTransport(body={"gl_code": "6400"}), "invalid schema"),
        (
            TwinTransport(response_url="http://attacker.invalid/api/expense-policy"),
            "undeclared host",
        ),
    ],
)
def test_lookup_fails_closed(transport, message):
    with pytest.raises(ToolExecutionError, match=message):
        lookup_expense_policy(manifest(), merchant="Acme", amount=10, transport=transport)


def test_selftest_runs_against_injected_twin():
    transport = TwinTransport()
    result = selftest_expense_policy_tool(manifest(), transport)
    assert result.passed
    assert len(transport.calls) == 1
