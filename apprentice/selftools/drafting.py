"""Deterministically draft and hash-pin the expense-policy self-tool."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from apprentice.canonical import digest
from apprentice.selftools.models import CapabilityRequest, DraftArtifact, ReadOnlyHttpManifest

_SERVER = '''"""Generated, constrained expense-policy lookup tool."""

from apprentice.selftools.runtime import lookup_expense_policy

__all__ = ["lookup_expense_policy"]
'''


def _manifest_text(manifest: ReadOnlyHttpManifest) -> str:
    return (
        f'name = "{manifest.name}"\n'
        f'origin = "{manifest.origin}"\n'
        f'risk = "{manifest.risk}"\n'
        f'base_url = "{str(manifest.base_url).rstrip("/")}"\n'
        f'path = "{manifest.path}"\n'
        f'method = "{manifest.method}"\n'
        f"timeout_seconds = {manifest.timeout_seconds:g}\n"
    )


def draft_expense_policy_tool(
    workspace: str | Path,
    request: CapabilityRequest,
    *,
    base_url: str,
    timeout_seconds: float = 3,
) -> DraftArtifact:
    """Write exactly two deterministic files below a request-specific jailed directory."""

    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = (root / request.request_id).resolve()
    if directory.parent != root:
        raise ValueError("draft directory escapes workspace")
    if directory.exists():
        raise FileExistsError(f"draft already exists: {request.request_id}")
    manifest = ReadOnlyHttpManifest(
        name=request.name, base_url=base_url, timeout_seconds=timeout_seconds
    )
    directory.mkdir(mode=0o700)
    contents = {"manifest.toml": _manifest_text(manifest), "server.py": _SERVER}
    for name, content in contents.items():
        (directory / name).write_text(content, encoding="utf-8")
    digests = {
        name: hashlib.sha256(content.encode()).hexdigest() for name, content in contents.items()
    }
    artifact_digest = digest(digests)
    return DraftArtifact(
        request=request,
        directory=directory,
        manifest=manifest,
        file_sha256=digests,
        sha256=artifact_digest,
    )


def verify_artifact(artifact: DraftArtifact) -> bool:
    """Verify exact filenames, bytes, manifest schema, and the review-time hash pin."""

    directory = artifact.directory.resolve()
    if directory != artifact.directory or not directory.is_dir():
        return False
    files = {path.name for path in directory.iterdir() if path.is_file()}
    if files != {"manifest.toml", "server.py"}:
        return False
    digests = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in sorted(files)
    }
    if digests != artifact.file_sha256 or digest(digests) != artifact.sha256:
        return False
    try:
        parsed = ReadOnlyHttpManifest.model_validate(
            tomllib.loads((directory / "manifest.toml").read_text(encoding="utf-8"))
        )
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return False
    return parsed == artifact.manifest
