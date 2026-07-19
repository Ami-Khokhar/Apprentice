"""Validated configuration for the Apprentice harness."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Never

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _FrozenDict(dict[Any, Any]):
    def _reject_mutation(self, *args: object, **kwargs: object) -> Never:
        raise TypeError("frozen policy mappings cannot be mutated")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __ior__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation


class SeverityDemotionPolicy(_FrozenModel):
    major: int = Field(ge=1, le=4)
    critical: int = Field(ge=1, le=4)

    @model_validator(mode="after")
    def matches_safety_invariant(self) -> "SeverityDemotionPolicy":
        if (self.major, self.critical) != (1, 2):
            raise ValueError("severity demotion must remain major=1 and critical=2")
        return self


class SignalWeightsPolicy(_FrozenModel):
    rubber_stamp: float = Field(gt=0)
    default: float = Field(gt=0)
    engaged: float = Field(gt=0)

    @model_validator(mode="after")
    def weights_are_ordered(self) -> "SignalWeightsPolicy":
        if not self.rubber_stamp <= self.default <= self.engaged:
            raise ValueError("signal weights must satisfy rubber_stamp <= default <= engaged")
        return self


class RiskPolicy(_FrozenModel):
    sample_mult: float = Field(ge=1)
    score_bonus: float = Field(ge=0, le=1)
    max_level: int = Field(ge=0, le=4)


class LevelPolicy(_FrozenModel):
    min_training_demonstrations: int | None = Field(default=None, gt=0)
    min_unique_shadow_passes: int | None = Field(default=None, gt=0)
    min_approved_successes: int | None = Field(default=None, gt=0)
    min_l3_successes: int | None = Field(default=None, gt=0)
    min_score: float | None = Field(default=None, gt=0, le=1)
    max_vetoes_recent: int | None = Field(default=None, ge=0)


_LEVEL_FIELD_SHAPES = {
    1: frozenset({"min_training_demonstrations"}),
    2: frozenset({"min_unique_shadow_passes", "min_score"}),
    3: frozenset({"min_approved_successes", "min_score", "max_vetoes_recent"}),
    4: frozenset({"min_l3_successes", "min_score"}),
}


class Policy(_FrozenModel):
    half_life_days: float = Field(gt=0)
    decay_idle_days: float = Field(gt=0)
    severity_demotion: SeverityDemotionPolicy
    signal_weights: SignalWeightsPolicy
    twin_pass_weight: float = Field(ge=0, le=1)
    veto_seconds: float = Field(ge=0)
    risk: dict[str, RiskPolicy]
    levels: dict[int, LevelPolicy]

    @field_validator("risk")
    @classmethod
    def validate_and_freeze_risk(cls, value: dict[str, RiskPolicy]) -> dict[str, RiskPolicy]:
        expected = {"low", "medium", "high", "critical"}
        if set(value) != expected:
            raise ValueError(f"risk classes must be exactly {sorted(expected)}")
        if value["critical"].max_level > 3:
            raise ValueError("critical risk max_level cannot exceed 3")
        return _FrozenDict(value)

    @field_validator("levels")
    @classmethod
    def validate_and_freeze_levels(cls, value: dict[int, LevelPolicy]) -> dict[int, LevelPolicy]:
        expected = {1, 2, 3, 4}
        if set(value) != expected:
            raise ValueError(f"policy levels must be exactly {sorted(expected)}")
        for level, requirements in value.items():
            expected_fields = _LEVEL_FIELD_SHAPES[level]
            configured_fields = requirements.model_fields_set
            has_null_value = any(
                getattr(requirements, field_name) is None for field_name in expected_fields
            )
            if configured_fields != expected_fields or has_null_value:
                raise ValueError(
                    f"level {level} requirements must be exactly {sorted(expected_fields)}"
                )
        return _FrozenDict(value)


def load_policy(path: str | Path) -> Policy:
    """Load and strictly validate a trust policy YAML file."""

    policy_path = Path(path)
    raw: Any = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("trust policy must contain a YAML mapping")
    return Policy.model_validate(raw)
