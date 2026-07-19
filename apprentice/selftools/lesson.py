"""The bounded, interactive policy-expense lesson exposed by the sidecar."""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from apprentice.config import Policy
from apprentice.induction.capability_gap import EXPENSE_POLICY_OUTPUTS, detect_expense_policy_gap
from apprentice.ledger.repository import Repository, UnknownBucketError
from apprentice.ledger.transitions import confirm_promotion
from apprentice.selftools.drafting import draft_expense_policy_tool
from apprentice.selftools.models import DraftArtifact, ExpensePolicy
from apprentice.selftools.runtime import HttpResponse
from apprentice.selftools.service import SelfToolService
from apprentice.sidecar.run_service import RunService
from demo_portal.app import build_portal


class _PortalTransport:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def request(self, *, method: str, url: str, timeout: float) -> HttpResponse:
        del timeout
        parsed = urlsplit(url)
        response = self.client.request(method, f"{parsed.path}?{parsed.query}")
        return HttpResponse(status=response.status_code, body=response.json(), url=url)


class PolicyExpenseLesson:
    """Teach and test one reviewable, read-only capability; never arbitrary code."""

    task = "File a policy-compliant expense using the accounting policy lookup."

    def __init__(self, repository: Repository, runs: RunService, policy: Policy) -> None:
        self.repository = repository
        self.runs = runs
        self.policy = policy
        self.tools = SelfToolService(repository, runs)
        self.client = TestClient(build_portal())
        self.artifact: DraftArtifact | None = None
        self.playbook_id: int | None = None
        self.last_policy: ExpensePolicy | None = None

    def teach(self) -> dict[str, object]:
        if self.artifact is not None:
            return self.status()
        existing = self._tool_bucket()
        request = detect_expense_policy_gap(
            available_fields=("merchant", "amount", "receipt"),
            required_fields=EXPENSE_POLICY_OUTPUTS,
        )
        assert request is not None
        self.artifact = draft_expense_policy_tool(
            Path(tempfile.mkdtemp(prefix="apprentice-lesson-")),
            request,
            base_url="http://policy.local",
        )
        if existing is not None:
            return self.status()
        tool = self.tools.activate(self.artifact, twin_transport=_PortalTransport(self.client))

        playbook = self.repository.create_bucket(name="policy-aware-expense")
        for name in ("policy-training-1", "policy-training-2"):
            self.repository.add_demonstration(playbook.id, name, name, "training")
        self.repository.add_demonstration(
            playbook.id, "policy-heldout", "policy-heldout", "heldout"
        )
        playbook = self.runs.activate_capability(
            playbook.id,
            reviewed_playbook={"task": self.task, "needed_tools": [tool.name]},
        )
        self.runs.record_shadow_result(
            bucket_id=playbook.id,
            playbook_version=playbook.playbook_version,
            heldout_artifact_digest="policy-heldout",
            passed=True,
        )
        playbook = confirm_promotion(self.repository, playbook.id, self.policy)
        self.runs.declare_tool_dependency(
            playbook.id, tool_name=tool.name, artifact_digest=self.artifact.sha256
        )
        self.playbook_id = playbook.id
        return self.status()

    def test(self) -> dict[str, object]:
        if self.artifact is None:
            if self._tool_bucket() is None:
                raise ValueError("Teach the policy-expense lesson before testing it.")
            self.teach()
        tool = self.repository.get_bucket_by_name(self.artifact.manifest.name)
        if tool.level < 2:
            self.repository.add_demonstration(
                tool.id, "tool-heldout-policy-v1", "tool-heldout-policy-v1", "heldout"
            )
            self.runs.record_shadow_result(
                bucket_id=tool.id,
                playbook_version=tool.playbook_version,
                heldout_artifact_digest="tool-heldout-policy-v1",
                passed=True,
            )
            confirm_promotion(self.repository, tool.id, self.policy)
        self.last_policy = self.tools.lookup_expense_policy(
            self.artifact,
            merchant="Contoso Travel",
            amount=1250,
            transport=_PortalTransport(self.client),
            approved=True,
        )
        return self.status()

    def status(self) -> dict[str, object]:
        tool = self._tool_bucket()
        return {
            "task": self.task,
            "taught": tool is not None,
            "tool_level": tool.level if tool is not None else None,
            "policy": self.last_policy.model_dump() if self.last_policy is not None else None,
        }

    def _tool_bucket(self):
        try:
            return self.repository.get_bucket_by_name("lookup_expense_policy")
        except UnknownBucketError:
            return None
