"""Structured practice-agent execution through the locally authenticated Codex CLI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agents import Agent
from pydantic import BaseModel, ValidationError

DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
DEFAULT_CODEX_TIMEOUT_SECONDS = 120.0

RunCommand = Callable[..., subprocess.CompletedProcess[str]]

_ALLOWED_ENVIRONMENT_VARIABLES = (
    "PATH",
    "HOME",
    "CODEX_HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "CODEX_CA_CERTIFICATE",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class CodexRunnerError(RuntimeError):
    """Base class for safe, actionable Codex runner failures."""


class CodexCLIUnavailableError(CodexRunnerError):
    """Raised when the Codex executable cannot be started."""


class CodexAuthenticationError(CodexRunnerError):
    """Raised when local Codex authentication is absent or expired."""


class CodexTimeoutError(CodexRunnerError):
    """Raised when Codex does not complete within the configured limit."""


class CodexExecutionError(CodexRunnerError):
    """Raised when Codex exits unsuccessfully for a non-authentication reason."""


class CodexOutputError(CodexRunnerError):
    """Raised when Codex does not return output matching the requested contract."""


class CodexStructuredRunner:
    """Run a tool-free, schema-constrained agent turn with ``codex exec``.

    The command executes in an empty temporary working directory and receives the
    full prompt over stdin, keeping learner content out of process arguments and
    preventing project instructions or workspace files from entering the turn.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
        run_command: RunCommand = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._run_command = run_command

    def __call__(self, agent: Agent[Any], prompt: str) -> BaseModel:
        output_type = agent.output_type
        if not isinstance(output_type, type) or not issubclass(output_type, BaseModel):
            raise TypeError("CodexStructuredRunner requires a Pydantic agent output_type")
        if agent.tools or agent.mcp_servers:
            raise ValueError("CodexStructuredRunner does not permit agent tools or MCP servers")
        if not isinstance(agent.instructions, str):
            raise TypeError("CodexStructuredRunner requires static string agent instructions")

        with tempfile.TemporaryDirectory(prefix="apprentice-codex-") as directory:
            workdir = Path(directory)
            schema_path = workdir / "output-schema.json"
            output_path = workdir / "last-message.json"
            schema_path.write_text(
                json.dumps(output_type.model_json_schema()), encoding="utf-8"
            )

            model = agent.model if isinstance(agent.model, str) else DEFAULT_CODEX_MODEL
            command = self._command(workdir, schema_path, output_path, model)
            request = self._request(agent, prompt)
            try:
                result = self._run_command(
                    command,
                    input=request,
                    text=True,
                    capture_output=True,
                    timeout=self._timeout_seconds,
                    check=False,
                    env=self._environment(),
                )
            except FileNotFoundError as exc:
                raise CodexCLIUnavailableError(
                    "Codex CLI was not found. Install Codex and ensure `codex` is on PATH."
                ) from exc
            except subprocess.TimeoutExpired:
                raise CodexTimeoutError(
                    f"Codex did not finish within {self._timeout_seconds:g} seconds. "
                    "Try again or increase the practice runner timeout."
                ) from None

            if result.returncode != 0:
                if self._looks_like_auth_failure(result.stderr):
                    raise CodexAuthenticationError(
                        "Codex authentication is unavailable or expired. Run `codex login` and "
                        "try again."
                    )
                raise CodexExecutionError(
                    f"Codex exited unsuccessfully (status {result.returncode}). "
                    "Run `codex exec --help` to verify the local CLI installation."
                )

            try:
                raw_output = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise CodexOutputError(
                    "Codex completed without writing its structured final response."
                ) from exc
            try:
                return output_type.model_validate_json(raw_output)
            except (ValidationError, ValueError):
                raise CodexOutputError(
                    f"Codex returned an invalid {output_type.__name__} response. Please retry."
                ) from None

    @staticmethod
    def _command(
        workdir: Path, schema_path: Path, output_path: Path, model: str
    ) -> Sequence[str]:
        return (
            "codex",
            "exec",
            "--model",
            model,
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--disable",
            "shell_tool",
            "--disable",
            "apps",
            "--disable",
            "multi_agent",
            "--disable",
            "remote_plugin",
            "--disable",
            "browser_use",
            "--disable",
            "in_app_browser",
            "--disable",
            "image_generation",
            "--config",
            'model_reasoning_effort="medium"',
            "--config",
            'web_search="disabled"',
            "--cd",
            str(workdir),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        )

    @staticmethod
    def _environment() -> dict[str, str]:
        """Return only environment values required to locate and authenticate Codex."""
        return {
            name: os.environ[name]
            for name in _ALLOWED_ENVIRONMENT_VARIABLES
            if name in os.environ
        }

    @staticmethod
    def _request(agent: Agent[Any], prompt: str) -> str:
        return (
            f"Agent instructions:\n{agent.instructions}\n\n"
            "Execution constraints:\n"
            "Do not use tools, web search, or inspect the filesystem or workspace. "
            "Everything needed is in the input below. Return only the JSON value required by "
            "the supplied output schema.\n\n"
            f"Input:\n{prompt}"
        )

    @staticmethod
    def _looks_like_auth_failure(stderr: str) -> bool:
        lowered = stderr.lower()
        return any(
            marker in lowered
            for marker in (
                "codex login",
                "not logged in",
                "login required",
                "please login",
                "authentication",
                "unauthorized",
                "token expired",
                "invalid api key",
                "missing api key",
                "401",
            )
        )
