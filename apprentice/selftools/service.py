"""Trust-aware activation and execution boundary for self-authored tools."""

from __future__ import annotations

from apprentice.ledger.repository import Repository
from apprentice.models import Bucket, BucketKind
from apprentice.selftools.drafting import verify_artifact
from apprentice.selftools.models import DraftArtifact, ExpensePolicy
from apprentice.selftools.runtime import (
    HttpTransport,
    ToolExecutionError,
    lookup_expense_policy,
    selftest_expense_policy_tool,
)
from apprentice.sidecar.run_service import RunService


class ToolAuthorityError(RuntimeError):
    """A self-tool call was not authorized by its current trust level."""


class SelfToolService:
    """Keep artifact integrity, trust, and live execution in one boundary."""

    def __init__(self, repository: Repository, run_service: RunService) -> None:
        self.repository = repository
        self.run_service = run_service

    def activate(
        self,
        artifact: DraftArtifact,
        *,
        twin_transport: HttpTransport,
    ) -> Bucket:
        if not verify_artifact(artifact):
            raise ToolAuthorityError("reviewed tool artifact no longer matches its hash")
        selftest = selftest_expense_policy_tool(artifact.manifest, twin_transport)
        if not selftest.passed:
            raise ToolAuthorityError(f"tool self-test failed: {selftest.detail}")
        return self.run_service.register_self_authored_tool(
            artifact.manifest.name,
            artifact_digest=artifact.sha256,
            target_base_url=str(artifact.manifest.base_url).rstrip("/"),
            approved_hosts=[artifact.manifest.base_url.host or ""],
        )

    def lookup_expense_policy(
        self,
        artifact: DraftArtifact,
        *,
        merchant: str,
        amount: float,
        transport: HttpTransport,
        approved: bool = False,
    ) -> ExpensePolicy:
        if not verify_artifact(artifact):
            raise ToolAuthorityError("tool artifact changed after review")
        bucket = self.repository.get_bucket_by_name(artifact.manifest.name)
        if bucket.kind is not BucketKind.TOOL or bucket.playbook_digest != artifact.sha256:
            raise ToolAuthorityError("tool is not registered at the reviewed artifact hash")
        if bucket.level < 2:
            raise ToolAuthorityError("L1 probation tools are restricted to twin self-tests")
        if bucket.level == 2 and not approved:
            raise ToolAuthorityError("L2 tool calls require explicit approval")
        try:
            return lookup_expense_policy(
                artifact.manifest,
                merchant=merchant,
                amount=amount,
                transport=transport,
            )
        except ToolExecutionError:
            self.repository.record_self_tool_failure(
                bucket.id,
                detail={
                    "cause": "tool_schema_or_transport_failure",
                    "merchant": merchant,
                    "amount": amount,
                },
            )
            raise
