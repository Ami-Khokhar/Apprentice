"""Safe, deterministic publication of retained demonstration artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from apprentice.canonical import digest
from apprentice.recorder.redact import DEFAULT_SENSITIVE_KEYS, sanitize_value

ARTIFACT_FILENAME = "artifact.json"
REQUIRED_KEYS = frozenset(
    {"task", "allowed_hosts", "steps", "http_entries", "response_templates"}
)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ArtifactSafetyError(ValueError):
    """Raised when retained output would contain explicitly forbidden content."""


class ArtifactIntegrityError(ValueError):
    """Raised when an artifact digest or referenced file does not match."""


def compute_artifact_digest(artifact: Mapping[str, Any]) -> str:
    """Compute the digest without making the digest field self-referential."""

    payload = dict(artifact)
    payload.pop("artifact_digest", None)
    return digest(payload)


def _derived_response_templates(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for entry in artifact.get("http_entries", []):
        if not isinstance(entry, Mapping):
            raise ArtifactIntegrityError("HTTP entries must be objects")
        method = entry.get("method")
        if entry.get("source") != "observed" or method not in _MUTATING_METHODS:
            continue
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, Mapping) or not isinstance(response, Mapping):
            raise ArtifactIntegrityError(
                "observed HTTP entries require request and response objects"
            )
        template_body = {
            "request": {
                "method": method,
                "url": entry.get("url"),
                "headers": request.get("headers"),
                "body": request.get("body"),
            },
            "response": response,
            "source": "observed",
        }
        templates.append({"id": digest(template_body), **template_body})
    return templates


def _validate_response_templates(artifact: Mapping[str, Any]) -> None:
    actual = artifact.get("response_templates")
    expected = _derived_response_templates(artifact)
    if actual != expected:
        raise ArtifactIntegrityError(
            "response templates must exactly derive from observed mutating HTTP entries"
        )


def finalize_artifact(
    artifact: Mapping[str, Any],
    *,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
    secret_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Apply the final sanitization pass and attach a deterministic digest."""

    missing = REQUIRED_KEYS - artifact.keys()
    if missing:
        raise ArtifactIntegrityError(f"artifact is missing required keys: {sorted(missing)}")
    clean = sanitize_value(
        dict(artifact),
        sensitive_keys=sensitive_keys,
        secret_values=secret_values,
    )
    clean.pop("artifact_digest", None)
    if not isinstance(clean["task"], str) or not clean["task"].strip():
        raise ArtifactIntegrityError("artifact task must be a non-empty string")
    for key in ("allowed_hosts", "steps", "http_entries", "response_templates"):
        if not isinstance(clean[key], list):
            raise ArtifactIntegrityError(f"artifact {key} must be a list")
    clean["allowed_hosts"] = sorted(set(clean["allowed_hosts"]))
    _validate_response_templates(clean)
    clean["artifact_digest"] = compute_artifact_digest(clean)
    return clean


def _safe_relative_path(value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ArtifactIntegrityError(f"unsafe artifact path: {value!r}")
    return Path(*relative.parts)


def _screenshot_references(artifact: Mapping[str, Any]) -> dict[str, str]:
    references: dict[str, str] = {}
    for step in artifact.get("steps", []):
        if not isinstance(step, Mapping):
            continue
        screenshot = step.get("screenshot")
        if not isinstance(screenshot, Mapping):
            continue
        path = screenshot.get("path")
        sha256 = screenshot.get("sha256")
        if isinstance(path, str) and isinstance(sha256, str):
            references[path] = sha256
    return references


def find_sentinels(root: str | Path, sentinels: Iterable[str]) -> dict[Path, tuple[str, ...]]:
    """Return every retained file containing any literal sentinel."""

    root_path = Path(root)
    needles = {sentinel: sentinel.encode("utf-8") for sentinel in sentinels if sentinel}
    matches: dict[Path, tuple[str, ...]] = {}
    if not root_path.exists():
        return matches
    paths = (
        [root_path]
        if root_path.is_file()
        else sorted(path for path in root_path.rglob("*") if path.is_file())
    )
    for path in paths:
        content = path.read_bytes()
        found = tuple(sorted(sentinel for sentinel, needle in needles.items() if needle in content))
        if found:
            matches[path] = found
    return matches


def assert_no_sentinels(root: str | Path, sentinels: Iterable[str]) -> None:
    """Raise if a forbidden literal occurs anywhere under a retained path."""

    matches = find_sentinels(root, sentinels)
    if matches:
        detail = ", ".join(f"{path}: {values}" for path, values in matches.items())
        raise ArtifactSafetyError(f"retained artifact contains forbidden sentinels: {detail}")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def publish_artifact(
    output_dir: str | Path,
    artifact: Mapping[str, Any],
    *,
    screenshots: Mapping[str, bytes] | None = None,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
    secret_values: Iterable[str] = (),
    overwrite: bool = True,
) -> Path:
    """Finalize into a temporary directory, scan it, then publish it as one tree."""

    target = Path(output_dir)
    if target == target.parent:
        raise ArtifactIntegrityError("refusing to publish an artifact at a filesystem root")
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = finalize_artifact(
        artifact,
        sensitive_keys=sensitive_keys,
        secret_values=secret_values,
    )
    screenshot_bytes = dict(screenshots or {})
    references = _screenshot_references(clean)
    if set(references) != set(screenshot_bytes):
        raise ArtifactIntegrityError("screenshot references and retained screenshot files differ")
    for relative, expected_digest in references.items():
        _safe_relative_path(relative)
        actual_digest = hashlib.sha256(screenshot_bytes[relative]).hexdigest()
        if actual_digest != expected_digest:
            raise ArtifactIntegrityError(f"screenshot digest does not match: {relative}")

    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        (staging / ARTIFACT_FILENAME).write_bytes(_json_bytes(clean))
        for relative, content in sorted(screenshot_bytes.items()):
            destination = staging / _safe_relative_path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        assert_no_sentinels(staging, secret_values)

        if target.exists():
            if not overwrite:
                raise FileExistsError(target)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target / ARTIFACT_FILENAME


def load_artifact(path: str | Path, *, verify: bool = True) -> dict[str, Any]:
    """Load an artifact directory or JSON file and optionally verify its digest."""

    artifact_path = Path(path)
    if artifact_path.is_dir():
        artifact_path /= ARTIFACT_FILENAME
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if verify:
        expected = artifact.get("artifact_digest")
        actual = compute_artifact_digest(artifact)
        if expected != actual:
            raise ArtifactIntegrityError(
                f"artifact digest mismatch: expected {expected!r}, computed {actual!r}"
            )
        _validate_response_templates(artifact)
        references = _screenshot_references(artifact)
        for relative, expected_digest in references.items():
            screenshot_path = artifact_path.parent / _safe_relative_path(relative)
            if not screenshot_path.is_file():
                raise ArtifactIntegrityError(f"referenced screenshot is missing: {relative}")
            actual_digest = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                raise ArtifactIntegrityError(f"screenshot digest mismatch: {relative}")
    return artifact


def retained_tree_digest(root: str | Path) -> str:
    """Hash relative paths and file bytes to compare independently generated trees."""

    root_path = Path(root)
    hasher = hashlib.sha256()
    for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
        relative = path.relative_to(root_path).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        content = path.read_bytes()
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()
