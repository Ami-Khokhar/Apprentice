"""Run the self-authored expense-policy tool lifecycle without external APIs."""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from apprentice.config import load_policy
from apprentice.induction.capability_gap import (
    EXPENSE_POLICY_OUTPUTS,
    detect_expense_policy_gap,
)
from apprentice.ledger.repository import Repository
from apprentice.ledger.transitions import confirm_promotion
from apprentice.selftools import (
    HttpResponse,
    SelfToolService,
    ToolAuthorityError,
    ToolExecutionError,
    draft_expense_policy_tool,
)
from apprentice.sidecar.console import build_dashboard_context
from apprentice.sidecar.run_service import RunService
from demo_portal.app import build_portal

ROOT = Path(__file__).parents[1]


class PortalTransport:
    def __init__(self, client: TestClient, *, drifted: bool = False) -> None:
        self.client = client
        self.drifted = drifted

    def request(self, *, method: str, url: str, timeout: float) -> HttpResponse:
        del timeout
        parsed = urlsplit(url)
        response = self.client.request(method, f"{parsed.path}?{parsed.query}")
        body = {"unexpected_schema": True} if self.drifted else response.json()
        return HttpResponse(status=response.status_code, body=body, url=url)


def _promote_with_heldout(
    repository: Repository,
    runs: RunService,
    *,
    bucket_id: int,
    artifact_digest: str,
) -> None:
    bucket = repository.get_bucket_by_id(bucket_id)
    repository.add_demonstration(
        bucket_id,
        f"heldout-{bucket.name}",
        artifact_digest,
        "heldout",
    )
    runs.record_shadow_result(
        bucket_id=bucket_id,
        playbook_version=bucket.playbook_version,
        heldout_artifact_digest=artifact_digest,
        passed=True,
    )
    confirm_promotion(repository, bucket_id, load_policy(ROOT / "trust_policy.yaml"))


def _say(message: str) -> None:
    print(f"\n>>> {message}")


def main() -> None:
    policy = load_policy(ROOT / "trust_policy.yaml")
    with tempfile.TemporaryDirectory(prefix="apprentice-selftool-") as temporary:
        root = Path(temporary)
        repository = Repository(root / "apprentice.db")
        runs = RunService(repository, policy=policy)
        tool_service = SelfToolService(repository, runs)

        request = detect_expense_policy_gap(
            available_fields=("merchant", "amount", "receipt"),
            required_fields=EXPENSE_POLICY_OUTPUTS,
        )
        assert request is not None
        _say(f"Capability gap detected: {request.reason}")

        artifact = draft_expense_policy_tool(
            root / "drafts",
            request,
            base_url="http://policy.local",
        )
        _say(
            "Drafted inert read-only tool: manifest.toml + server.py; "
            f"review hash {artifact.sha256}"
        )

        with TestClient(build_portal()) as portal:
            transport = PortalTransport(portal)
            tool = tool_service.activate(artifact, twin_transport=transport)
            _say(f"Twin self-test passed; {tool.name} entered probation at L{tool.level}")

            playbook = repository.create_bucket(name="policy-aware-expense")
            repository.add_demonstration(playbook.id, "training-1", "policy-training-1", "training")
            repository.add_demonstration(playbook.id, "training-2", "policy-training-2", "training")
            repository.add_demonstration(playbook.id, "heldout-parent", "policy-heldout", "heldout")
            playbook = runs.activate_capability(
                playbook.id,
                reviewed_playbook={
                    "task": "file a policy-compliant expense",
                    "needed_tools": [tool.name],
                },
            )
            runs.record_shadow_result(
                bucket_id=playbook.id,
                playbook_version=playbook.playbook_version,
                heldout_artifact_digest="policy-heldout",
                passed=True,
            )
            playbook = confirm_promotion(repository, playbook.id, policy)
            playbook = runs.declare_tool_dependency(
                playbook.id,
                tool_name=tool.name,
                artifact_digest=artifact.sha256,
            )
            _say(f"Playbook reached L{playbook.level} and pinned tool hash {artifact.sha256[:12]}…")

            try:
                tool_service.lookup_expense_policy(
                    artifact, merchant="Contoso Travel", amount=1250, transport=transport
                )
            except ToolAuthorityError as error:
                _say(f"Expected probation denial: {error}")

            _promote_with_heldout(
                repository,
                runs,
                bucket_id=tool.id,
                artifact_digest="tool-heldout-policy-v1",
            )
            tool = repository.get_bucket_by_id(tool.id)
            _say(
                f"Held-out self-test promoted the tool to L{tool.level}; "
                "first live call needs approval"
            )

            result = tool_service.lookup_expense_policy(
                artifact,
                merchant="Contoso Travel",
                amount=1250,
                transport=transport,
                approved=True,
            )
            _say(
                "Approved policy lookup returned "
                f"{result.category}/{result.gl_code}/{result.cost_center}; "
                f"approval_required={result.approval_required}"
            )

            portal.post(
                "/login",
                data={"username": "demo", "password": "not-recorded"},
            )
            response = portal.post(
                "/expense?policy=1",
                data={
                    "merchant": "Contoso Travel",
                    "amount": "1250",
                    "category": result.category,
                    "gl_code": result.gl_code,
                    "cost_center": result.cost_center,
                    "approval_route": "manager" if result.approval_required else "auto",
                    "justification": "Customer onsite visit",
                },
                files={"receipt": ("receipt.pdf", b"demo receipt", "application/pdf")},
            )
            assert response.status_code == 201
            _say("Policy-compliant expense committed exactly once")

            before = build_dashboard_context(repository, policy)["capabilities"]
            parent_before = next(card for card in before if card["name"] == playbook.name)
            _say(f"Composed effective trust before drift: L{parent_before['effective_level']}")

            try:
                tool_service.lookup_expense_policy(
                    artifact,
                    merchant="Contoso Travel",
                    amount=1250,
                    transport=PortalTransport(portal, drifted=True),
                    approved=True,
                )
            except ToolExecutionError as error:
                _say(f"Changed policy schema failed closed: {error}")

            after = build_dashboard_context(repository, policy)["capabilities"]
            parent_after = next(card for card in after if card["name"] == playbook.name)
            tool_after = repository.get_bucket_by_id(tool.id)
            _say(
                f"Tool demoted to L{tool_after.level}; dependent playbook effective trust "
                f"fell to L{parent_after['effective_level']}"
            )


if __name__ == "__main__":
    main()
