"""The hosted runner must use the learner's key and translate API faults safely."""

from __future__ import annotations

import openai
import pytest
from pydantic import BaseModel

from apprentice.practice.codex_runner import (
    AgentSpec,
    CodexAuthenticationError,
    CodexExecutionError,
    CodexOutputError,
    CodexTimeoutError,
)
from apprentice.practice.openai_runner import (
    CURRENT_API_KEY,
    MissingAPIKeyError,
    OpenAIStructuredRunner,
)


class Verdict(BaseModel):
    summary: str


AGENT = AgentSpec(
    name="debriefer", instructions="Be terse.", model="test-model", output_type=Verdict
)


class _Response:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


def _client_factory(monkeypatch, *, parsed: object = None, error: Exception | None = None):
    """Install a stub OpenAI client and record the key and arguments it was given."""
    seen: dict[str, object] = {}

    class _Parses:
        def parse(self, **kwargs):
            seen.update(kwargs)
            if error is not None:
                raise error
            return _Response(parsed)

    class _Client:
        def __init__(self, **kwargs):
            seen["api_key"] = kwargs.get("api_key")
            self.responses = _Parses()

    monkeypatch.setattr(openai, "OpenAI", _Client)
    return seen


def _api_error(kind: type[Exception]) -> Exception:
    return kind.__new__(kind)


def test_uses_the_key_from_the_request_context(monkeypatch) -> None:
    seen = _client_factory(monkeypatch, parsed=Verdict(summary="ok"))
    token = CURRENT_API_KEY.set("sk-learner")
    try:
        result = OpenAIStructuredRunner()(AGENT, "What now?")
    finally:
        CURRENT_API_KEY.reset(token)

    assert result == Verdict(summary="ok")
    assert seen["api_key"] == "sk-learner"
    assert seen["model"] == "test-model"
    assert seen["instructions"] == "Be terse."


def test_missing_key_is_reported_as_authentication(monkeypatch) -> None:
    _client_factory(monkeypatch, parsed=Verdict(summary="ok"))
    with pytest.raises(MissingAPIKeyError):
        OpenAIStructuredRunner()(AGENT, "What now?")


def test_missing_key_error_is_an_authentication_error() -> None:
    assert issubclass(MissingAPIKeyError, CodexAuthenticationError)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (openai.AuthenticationError, CodexAuthenticationError),
        (openai.PermissionDeniedError, CodexAuthenticationError),
        (openai.APITimeoutError, CodexTimeoutError),
        (openai.RateLimitError, CodexExecutionError),
        (openai.APIConnectionError, CodexExecutionError),
    ],
)
def test_api_faults_become_actionable_practice_errors(monkeypatch, raised, expected) -> None:
    _client_factory(monkeypatch, error=_api_error(raised))
    token = CURRENT_API_KEY.set("sk-learner")
    try:
        with pytest.raises(expected) as failure:
            OpenAIStructuredRunner()(AGENT, "What now?")
    finally:
        CURRENT_API_KEY.reset(token)

    assert "sk-learner" not in str(failure.value)


def test_unparsed_output_is_reported_as_an_output_fault(monkeypatch) -> None:
    _client_factory(monkeypatch, parsed=None)
    token = CURRENT_API_KEY.set("sk-learner")
    try:
        with pytest.raises(CodexOutputError):
            OpenAIStructuredRunner()(AGENT, "What now?")
    finally:
        CURRENT_API_KEY.reset(token)
