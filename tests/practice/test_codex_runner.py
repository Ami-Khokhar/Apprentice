from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from apprentice.practice.codex_runner import (
    AgentSpec,
    CodexAuthenticationError,
    CodexCLIUnavailableError,
    CodexExecutionError,
    CodexOutputError,
    CodexStructuredRunner,
    CodexTimeoutError,
)
from apprentice.practice.contracts import FacilitatorDecision


class Result(BaseModel):
    answer: str


class FakeCommand:
    def __init__(self, *, output: str = '{"answer":"steady"}', stderr: str = "") -> None:
        self.output = output
        self.stderr = stderr
        self.returncode = 0
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(self.output, encoding="utf-8")
        return subprocess.CompletedProcess(
            command, self.returncode, stdout="ignored", stderr=self.stderr
        )


def agent() -> AgentSpec:
    return AgentSpec(
        name="test-agent",
        instructions="Use only canonical evidence.",
        model="gpt-5.6-terra",
        output_type=Result,
    )


def test_runner_uses_isolated_hardened_codex_command_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("CODEX_HOME", "/safe/codex")
    monkeypatch.setenv("HTTPS_PROXY", "https://safe-proxy.example")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    fake = FakeCommand()

    result = CodexStructuredRunner(run_command=fake, timeout_seconds=17)(
        agent(), '{"learner_response":"Investigate safely"}'
    )

    assert result == Result(answer="steady")
    command, kwargs = fake.calls[0]
    assert command[:2] == ("codex", "exec")
    assert "gpt-5.6-terra" in command
    assert {"--ephemeral", "--skip-git-repo-check", "--ignore-user-config"} <= set(command)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--config") + 1] == 'model_reasoning_effort="medium"'
    disabled = {
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--disable"
    }
    assert disabled == {
        "shell_tool",
        "apps",
        "multi_agent",
        "remote_plugin",
        "browser_use",
        "in_app_browser",
        "image_generation",
    }
    config_values = {
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--config"
    }
    assert 'web_search="disabled"' in config_values
    assert command[-1] == "-"
    assert "Investigate safely" not in command
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 17
    assert set(kwargs["env"]) <= {
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
    }
    assert kwargs["env"]["PATH"] == "/safe/bin"
    assert kwargs["env"]["HOME"] == "/safe/home"
    assert kwargs["env"]["CODEX_HOME"] == "/safe/codex"
    assert kwargs["env"]["HTTPS_PROXY"] == "https://safe-proxy.example"
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "UNRELATED_SECRET" not in kwargs["env"]
    assert "Use only canonical evidence." in kwargs["input"]
    assert "Do not use tools, web search" in kwargs["input"]

    workdir = Path(command[command.index("--cd") + 1])
    schema_path = Path(command[command.index("--output-schema") + 1])
    assert schema_path.parent == workdir
    # Temporary execution material is removed after every turn.
    assert not workdir.exists()


def test_runner_honors_explicit_string_agent_model() -> None:
    fake = FakeCommand()
    configured = AgentSpec(
        name="test-agent",
        instructions="Use only canonical evidence.",
        model="explicit-test-model",
        output_type=Result,
    )

    CodexStructuredRunner(run_command=fake)(configured, "input")

    command, _ = fake.calls[0]
    assert command[command.index("--model") + 1] == "explicit-test-model"


def test_runner_writes_model_json_schema() -> None:
    seen_schema: dict[str, Any] = {}

    def run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        schema_path = Path(command[command.index("--output-schema") + 1])
        seen_schema.update(json.loads(schema_path.read_text(encoding="utf-8")))
        Path(command[command.index("--output-last-message") + 1]).write_text(
            '{"answer":"ok"}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    CodexStructuredRunner(run_command=run)(agent(), "input")

    assert seen_schema["properties"]["answer"]["type"] == "string"
    assert seen_schema["required"] == ["answer"]


def test_facilitator_schema_requires_nullable_fields_and_assessment_evidence() -> None:
    schema = FacilitatorDecision.model_json_schema()

    assert set(schema["required"]) == {
        "action_kind",
        "requires_clarification",
        "coaching_question",
        "assessment",
    }
    assert set(schema["$defs"]["DecisionAssessment"]["required"]) == {
        "response_excerpt",
        "interpretation",
        "strength",
        "risk",
        "evidence",
    }
    action_variants = schema["properties"]["action_kind"]["anyOf"]
    coaching_variants = schema["properties"]["coaching_question"]["anyOf"]
    assert {variant.get("type") for variant in action_variants} == {"string", "null"}
    assert {variant.get("type") for variant in coaching_variants} == {"string", "null"}


def test_missing_cli_has_actionable_safe_error() -> None:
    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("sensitive command context")

    with pytest.raises(CodexCLIUnavailableError, match="on PATH") as captured:
        CodexStructuredRunner(run_command=missing)(agent(), "private learner prompt")

    assert "private learner prompt" not in str(captured.value)


def test_auth_failure_does_not_expose_stderr_or_prompt() -> None:
    fake = FakeCommand(stderr="401 unauthorized token=secret private learner prompt")
    fake.returncode = 1

    with pytest.raises(CodexAuthenticationError, match="codex login") as captured:
        CodexStructuredRunner(run_command=fake)(agent(), "private learner prompt")

    assert "secret" not in str(captured.value)
    assert "private learner prompt" not in str(captured.value)


def test_timeout_has_actionable_safe_error() -> None:
    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr="secret")

    with pytest.raises(CodexTimeoutError, match="3 seconds") as captured:
        CodexStructuredRunner(timeout_seconds=3, run_command=timeout)(
            agent(), "private learner prompt"
        )

    assert "secret" not in str(captured.value)
    assert captured.value.__suppress_context__ is True


def test_nonzero_exit_does_not_expose_captured_stderr() -> None:
    fake = FakeCommand(stderr="internal failure secret")
    fake.returncode = 9

    with pytest.raises(CodexExecutionError, match="status 9") as captured:
        CodexStructuredRunner(run_command=fake)(agent(), "input")

    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("output", ["not-json", '{"wrong":"shape"}'])
def test_invalid_structured_output_is_clear(output: str) -> None:
    with pytest.raises(CodexOutputError, match="invalid Result response") as captured:
        CodexStructuredRunner(run_command=FakeCommand(output=output))(agent(), "input")

    assert captured.value.__suppress_context__ is True


def test_missing_last_message_is_clear() -> None:
    def no_output(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(CodexOutputError, match="without writing"):
        CodexStructuredRunner(run_command=no_output)(agent(), "input")
