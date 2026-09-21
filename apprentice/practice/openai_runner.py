"""Structured practice-agent execution through the OpenAI API with a learner's own key.

The hosted deployment has no Codex CLI and no shared account. Each learner supplies
an API key, which lives in this process only for the turn that uses it.
"""

from __future__ import annotations

from contextvars import ContextVar

from pydantic import BaseModel, ValidationError

from .codex_runner import (
    AgentSpec,
    CodexAuthenticationError,
    CodexExecutionError,
    CodexOutputError,
    CodexTimeoutError,
)

CURRENT_API_KEY: ContextVar[str] = ContextVar("apprentice_api_key", default="")
"""The key supplied for the request in flight. Never stored and never logged."""

DEFAULT_TIMEOUT_SECONDS = 120.0


class MissingAPIKeyError(CodexAuthenticationError):
    """Raised when a learner starts practice without supplying a key."""


class OpenAIStructuredRunner:
    """Run one tool-free, schema-constrained agent turn against the OpenAI API."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        api_key: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._api_key = api_key

    def __call__(self, agent: AgentSpec, prompt: str) -> BaseModel:
        output_type = agent.output_type
        if not isinstance(output_type, type) or not issubclass(output_type, BaseModel):
            raise TypeError("OpenAIStructuredRunner requires a Pydantic agent output_type")
        if not isinstance(agent.instructions, str):
            raise TypeError("OpenAIStructuredRunner requires static string agent instructions")

        key = self._api_key if self._api_key is not None else CURRENT_API_KEY.get()
        if not key:
            raise MissingAPIKeyError(
                "Add your OpenAI API key to start practising. It is held for this turn only."
            )

        import openai

        client = openai.OpenAI(api_key=key, timeout=self._timeout_seconds, max_retries=1)
        try:
            completion = client.responses.parse(
                model=agent.model,
                instructions=agent.instructions,
                input=prompt,
                text_format=output_type,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise CodexAuthenticationError(
                "That API key was rejected. Check the key and that it can reach the chosen model."
            ) from None
        except openai.APITimeoutError:
            raise CodexTimeoutError(
                f"The practice guide did not answer within {self._timeout_seconds:g} seconds. "
                "Try again."
            ) from None
        except openai.RateLimitError:
            raise CodexExecutionError(
                "Your account hit a rate or quota limit. Wait a moment, then try again."
            ) from None
        except openai.APIError:
            raise CodexExecutionError(
                "The practice guide could not be reached. Try again in a moment."
            ) from None

        parsed = completion.output_parsed
        if parsed is None:
            raise CodexOutputError(
                f"The practice guide returned no {output_type.__name__} response. Please retry."
            )
        try:
            return output_type.model_validate(parsed)
        except (ValidationError, ValueError):
            raise CodexOutputError(
                f"The practice guide returned an invalid {output_type.__name__} response. "
                "Please retry."
            ) from None
