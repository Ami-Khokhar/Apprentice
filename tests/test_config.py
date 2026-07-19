from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from openai.types.chat import ChatCompletion
from openai.types.completion_usage import CompletionUsage
from pydantic import ValidationError

from apprentice.config import load_policy
from apprentice.providers import (
    _normalize_nullable_usage,
    api_key_env,
    model_name,
    provider_model,
    provider_model_settings,
    provider_name,
)
from apprentice.sidecar.app import build_sidecar

ROOT = Path(__file__).parents[1]


def _policy_data() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "trust_policy.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _write_policy(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> Path:
    data = deepcopy(_policy_data())
    mutate(data)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_policy_reads_demo_thresholds() -> None:
    policy = load_policy(ROOT / "trust_policy.yaml")

    assert policy.half_life_days == 30
    assert policy.severity_demotion.critical == 2
    assert policy.risk["critical"].max_level == 3
    assert policy.levels[2].min_unique_shadow_passes == 1
    assert policy.levels[3].min_approved_successes == 2


def test_load_policy_rejects_unknown_fields(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text((ROOT / "trust_policy.yaml").read_text() + "unexpected_setting: true\n")

    with pytest.raises(ValidationError, match="unexpected_setting"):
        load_policy(policy_path)


def test_load_policy_rejects_missing_risk_class(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        """
half_life_days: 30
decay_idle_days: 21
severity_demotion: {major: 1, critical: 2}
signal_weights: {rubber_stamp: 0.3, default: 1.0, engaged: 1.2}
twin_pass_weight: 0.25
veto_seconds: 5
risk:
  low: {sample_mult: 1.0, score_bonus: 0, max_level: 4}
  medium: {sample_mult: 1.0, score_bonus: 0, max_level: 4}
  high: {sample_mult: 2.0, score_bonus: 0.03, max_level: 4}
levels:
  1: {min_training_demonstrations: 2}
  2: {min_unique_shadow_passes: 1, min_score: 0.85}
  3: {min_approved_successes: 2, min_score: 0.90, max_vetoes_recent: 1}
  4: {min_l3_successes: 5, min_score: 0.95}
""".strip()
    )

    with pytest.raises(ValidationError, match="risk classes"):
        load_policy(policy_path)


@pytest.mark.parametrize(
    ("level", "requirements"),
    [
        (1, {}),
        (1, {"min_training_demonstrations": 2, "min_score": 0.5}),
        (2, {"min_score": 0.85}),
        (2, {"min_approved_successes": 1, "min_score": 0.85}),
        (3, {"min_approved_successes": 2, "min_score": 0.9}),
        (4, {"min_approved_successes": 5, "min_score": 0.95}),
    ],
)
def test_load_policy_rejects_empty_or_wrong_level_shapes(
    tmp_path: Path,
    level: int,
    requirements: dict[str, int | float],
) -> None:
    def replace_level(data: dict[str, Any]) -> None:
        data["levels"][level] = requirements

    policy_path = _write_policy(tmp_path, replace_level)

    with pytest.raises(ValidationError, match=rf"level {level} requirements must be exactly"):
        load_policy(policy_path)


@pytest.mark.parametrize(
    ("risk_class", "field", "value", "message"),
    [
        ("low", "sample_mult", 0.99, "greater than or equal to 1"),
        ("critical", "max_level", 4, "critical risk max_level cannot exceed 3"),
    ],
)
def test_load_policy_rejects_unsafe_risk_configuration(
    tmp_path: Path,
    risk_class: str,
    field: str,
    value: int | float,
    message: str,
) -> None:
    def change_risk(data: dict[str, Any]) -> None:
        data["risk"][risk_class][field] = value

    policy_path = _write_policy(tmp_path, change_risk)

    with pytest.raises(ValidationError, match=message):
        load_policy(policy_path)


@pytest.mark.parametrize(
    "severity_demotion",
    [
        {"major": 2, "critical": 2},
        {"major": 1, "critical": 3},
    ],
)
def test_load_policy_rejects_changed_demotion_invariants(
    tmp_path: Path,
    severity_demotion: dict[str, int],
) -> None:
    def change_demotion(data: dict[str, Any]) -> None:
        data["severity_demotion"] = severity_demotion

    policy_path = _write_policy(tmp_path, change_demotion)

    with pytest.raises(ValidationError, match="major=1 and critical=2"):
        load_policy(policy_path)


def test_policy_nested_containers_are_immutable() -> None:
    policy = load_policy(ROOT / "trust_policy.yaml")

    with pytest.raises(TypeError):
        policy.risk["low"] = policy.risk["medium"]  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.risk.pop("low")
    with pytest.raises(TypeError):
        policy.levels[1] = policy.levels[2]  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.levels.pop(1)

    dumped = policy.model_dump()
    assert dumped["risk"]["critical"]["max_level"] == 3


def test_load_policy_rejects_unsafe_yaml_tags(tmp_path: Path) -> None:
    policy_path = tmp_path / "unsafe.yaml"
    policy_path.write_text(
        "!!python/object/apply:builtins.str ['unsafe']\n",
        encoding="utf-8",
    )

    with pytest.raises(yaml.constructor.ConstructorError):
        load_policy(policy_path)


def test_model_name_defaults_to_gpt_5_6(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPRENTICE_PROVIDER", raising=False)
    monkeypatch.delenv("APPRENTICE_MODEL", raising=False)

    assert model_name() == "gpt-5.6"


def test_model_name_uses_non_blank_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPRENTICE_PROVIDER", raising=False)
    monkeypatch.setenv("APPRENTICE_MODEL", "  test-model  ")
    assert model_name() == "test-model"

    monkeypatch.setenv("APPRENTICE_MODEL", "  ")
    assert model_name() == "gpt-5.6"


def test_provider_defaults_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPRENTICE_PROVIDER", raising=False)
    monkeypatch.delenv("APPRENTICE_MODEL", raising=False)

    assert provider_name() == "openai"
    assert api_key_env() == "OPENAI_API_KEY"
    assert provider_model() == "gpt-5.6"


@pytest.mark.parametrize(
    ("provider", "expected_model", "expected_key_env"),
    [
        ("gemini", "gemini-2.5-pro", "GEMINI_API_KEY"),
        ("groq", "openai/gpt-oss-120b", "GROQ_API_KEY"),
        ("nvidia", "nvidia/nemotron-3-super-120b-a12b", "NVIDIA_API_KEY"),
    ],
)
def test_non_openai_provider_builds_chat_completions_model(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    expected_model: str,
    expected_key_env: str,
) -> None:
    monkeypatch.setenv("APPRENTICE_PROVIDER", provider)
    monkeypatch.setenv(expected_key_env, "test-key")
    monkeypatch.delenv("APPRENTICE_MODEL", raising=False)

    model = provider_model()

    assert model_name() == expected_model
    assert api_key_env() == expected_key_env
    assert model.model == expected_model


def test_non_openai_provider_requires_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPRENTICE_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        provider_model()


def test_nvidia_provider_sets_agentic_chat_template_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPRENTICE_PROVIDER", "nvidia")

    settings = provider_model_settings()

    assert settings.extra_body == {
        "chat_template_kwargs": {"enable_thinking": True, "force_nonempty_content": True}
    }


def test_gemini_provider_uses_deterministic_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPRENTICE_PROVIDER", "gemini")

    assert provider_model_settings().temperature == 0


def test_vertex_nullable_tool_call_usage_is_normalized() -> None:
    usage = CompletionUsage.model_construct(
        prompt_tokens=100,
        completion_tokens=None,
        total_tokens=None,
    )
    response = ChatCompletion.model_construct(usage=usage)

    normalized = _normalize_nullable_usage(response)

    assert normalized.usage.prompt_tokens == 100
    assert normalized.usage.completion_tokens == 0
    assert normalized.usage.total_tokens == 0


def test_unknown_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPRENTICE_PROVIDER", "unknown")

    with pytest.raises(ValueError, match="unsupported APPRENTICE_PROVIDER"):
        provider_name()


def test_sidecar_wires_custom_policy_into_default_services(tmp_path: Path) -> None:
    policy = load_policy(ROOT / "trust_policy.yaml").model_copy(
        update={"veto_seconds": 99.0, "twin_pass_weight": 0.9}
    )

    app = build_sidecar(db_path=tmp_path / "apprentice.db", policy=policy)

    assert app.state.approval_service._veto_seconds == 99.0
    assert app.state.run_service._twin_pass_weight == 0.9
