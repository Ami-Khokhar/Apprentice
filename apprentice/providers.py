"""Inference-provider configuration for Apprentice agents."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, cast

from agents import (
    AsyncOpenAI,
    Model,
    ModelSettings,
    OpenAIChatCompletionsModel,
    set_tracing_disabled,
)

ProviderName = Literal["openai", "gemini", "groq", "nvidia"]


@dataclass(frozen=True)
class ProviderDefaults:
    model: str
    api_key_env: str
    base_url: str | None


PROVIDER_DEFAULTS: dict[ProviderName, ProviderDefaults] = {
    "openai": ProviderDefaults("gpt-5.6", "OPENAI_API_KEY", None),
    "gemini": ProviderDefaults(
        "gemini-2.5-pro",
        "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    "groq": ProviderDefaults(
        "openai/gpt-oss-120b",
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
    ),
    "nvidia": ProviderDefaults(
        "nvidia/nemotron-3-super-120b-a12b",
        "NVIDIA_API_KEY",
        "https://integrate.api.nvidia.com/v1",
    ),
}


def _normalize_nullable_usage(response: Any) -> Any:
    """Normalize Vertex's nullable intermediate tool-call usage counters."""

    usage = getattr(response, "usage", None)
    if usage is None:
        return response
    updates = {
        field: 0
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(usage, field, None) is None
    }
    if not updates:
        return response
    return response.model_copy(update={"usage": usage.model_copy(update=updates)})


class GeminiChatCompletionsModel(OpenAIChatCompletionsModel):
    """Chat Completions model tolerant of Vertex's nullable usage fields."""

    async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
        response = await super()._fetch_response(*args, **kwargs)
        if isinstance(response, tuple):
            normalized, stream = response
            return _normalize_nullable_usage(normalized), stream
        return _normalize_nullable_usage(response)


def provider_name() -> ProviderName:
    """Return the selected inference provider."""

    configured = os.getenv("APPRENTICE_PROVIDER", "openai").strip().lower()
    if configured not in PROVIDER_DEFAULTS:
        supported = ", ".join(PROVIDER_DEFAULTS)
        raise ValueError(f"unsupported APPRENTICE_PROVIDER {configured!r}; choose from {supported}")
    return cast(ProviderName, configured)


def model_name() -> str:
    """Return the configured model name for the selected provider."""

    configured = os.getenv("APPRENTICE_MODEL", "").strip()
    return configured or PROVIDER_DEFAULTS[provider_name()].model


def api_key_env() -> str:
    """Return the environment variable containing the selected provider's key."""

    return PROVIDER_DEFAULTS[provider_name()].api_key_env


def provider_model_settings() -> ModelSettings:
    """Return provider-specific request compatibility settings."""

    provider = provider_name()
    if provider == "gemini":
        return ModelSettings(temperature=0)
    if provider == "nvidia":
        return ModelSettings(
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": True,
                    "force_nonempty_content": True,
                }
            }
        )
    return ModelSettings()


def provider_model() -> str | Model:
    """Build the SDK model used by every Apprentice agent.

    OpenAI retains the SDK's native Responses API path. Other providers use
    their OpenAI-compatible Chat Completions endpoints, which are the common
    denominator for typed output and function calling.
    """

    provider = provider_name()
    if provider == "openai":
        return model_name()

    defaults = PROVIDER_DEFAULTS[provider]
    key = os.getenv(defaults.api_key_env, "").strip()
    if not key:
        raise RuntimeError(
            f"APPRENTICE_PROVIDER={provider} requires {defaults.api_key_env} to be set"
        )
    base_url = os.getenv("APPRENTICE_BASE_URL", "").strip() or defaults.base_url
    assert base_url is not None

    # The default trace exporter is OpenAI-hosted and requires an OpenAI key.
    set_tracing_disabled(True)
    client = AsyncOpenAI(api_key=key, base_url=base_url)
    model_type = GeminiChatCompletionsModel if provider == "gemini" else OpenAIChatCompletionsModel
    return model_type(model=model_name(), openai_client=client)
