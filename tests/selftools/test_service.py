from pathlib import Path

import pytest

from apprentice.config import load_policy
from apprentice.ledger.repository import Repository
from apprentice.ledger.transitions import confirm_promotion
from apprentice.selftools import (
    CapabilityRequest,
    HttpResponse,
    SelfToolService,
    ToolAuthorityError,
    ToolExecutionError,
    draft_expense_policy_tool,
)
from apprentice.sidecar.run_service import RunService

ROOT = Path(__file__).parents[2]


class Transport:
    def __init__(self, body: dict) -> None:
        self.body = body

    def request(self, *, method: str, url: str, timeout: float) -> HttpResponse:
        return HttpResponse(status=200, body=self.body, url=url)


def _request() -> CapabilityRequest:
    return CapabilityRequest(
        request_id="expense-policy-v1",
        name="lookup_expense_policy",
        reason="Accounting fields are not derivable from browser inputs.",
        required_outputs=(
            "category",
            "gl_code",
            "cost_center",
            "approval_required",
            "approval_route",
            "justification_threshold",
        ),
    )


def _valid_transport() -> Transport:
    return Transport(
        {
            "category": "office_supplies",
            "gl_code": "6400",
            "cost_center": "OPERATIONS",
            "approval_required": False,
            "approval_route": "auto",
            "justification_threshold": 1000,
        }
    )


def test_self_tool_lifecycle_enforces_probation_approval_and_failure_demotion(
    tmp_path: Path,
) -> None:
    policy = load_policy(ROOT / "trust_policy.yaml")
    repository = Repository(tmp_path / "ledger.db")
    runs = RunService(repository, policy=policy)
    artifact = draft_expense_policy_tool(
        tmp_path / "drafts", _request(), base_url="http://policy.local"
    )
    service = SelfToolService(repository, runs)

    tool = service.activate(artifact, twin_transport=_valid_transport())
    assert tool.level == 1
    with pytest.raises(ToolAuthorityError, match="L1 probation"):
        service.lookup_expense_policy(
            artifact, merchant="Acme", amount=10, transport=_valid_transport()
        )

    repository.add_demonstration(
        tool.id, "tool-heldout", "tool-heldout-digest", "heldout"
    )
    runs.record_shadow_result(
        bucket_id=tool.id,
        playbook_version=tool.playbook_version,
        heldout_artifact_digest="tool-heldout-digest",
        passed=True,
    )
    promoted = confirm_promotion(repository, tool.id, policy)
    assert promoted.level == 2
    with pytest.raises(ToolAuthorityError, match="explicit approval"):
        service.lookup_expense_policy(
            artifact, merchant="Acme", amount=10, transport=_valid_transport()
        )

    result = service.lookup_expense_policy(
        artifact,
        merchant="Acme",
        amount=10,
        transport=_valid_transport(),
        approved=True,
    )
    assert result.gl_code == "6400"

    with pytest.raises(ToolExecutionError, match="invalid schema"):
        service.lookup_expense_policy(
            artifact,
            merchant="Acme",
            amount=10,
            transport=Transport({"schema": "drifted"}),
            approved=True,
        )
    assert repository.get_bucket_by_id(tool.id).level == 1
