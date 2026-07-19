"""Strict data contracts for the only supported self-tool shape."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityRequest(_FrozenModel):
    request_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: Literal["lookup_expense_policy"]
    reason: str = Field(min_length=1, max_length=500)
    required_outputs: tuple[
        Literal[
            "category",
            "gl_code",
            "cost_center",
            "approval_required",
            "approval_route",
            "justification_threshold",
        ],
        ...,
    ]

    @field_validator("required_outputs")
    @classmethod
    def outputs_are_complete(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = {
            "category",
            "gl_code",
            "cost_center",
            "approval_required",
            "approval_route",
            "justification_threshold",
        }
        if set(value) != expected or len(value) != len(expected):
            raise ValueError("required_outputs must contain every policy field exactly once")
        return value


class ReadOnlyHttpManifest(_FrozenModel):
    name: Literal["lookup_expense_policy"]
    origin: Literal["self_authored"] = "self_authored"
    risk: Literal["low"] = "low"
    base_url: HttpUrl
    path: Literal["/api/expense-policy"] = "/api/expense-policy"
    method: Literal["GET"] = "GET"
    timeout_seconds: float = Field(gt=0, le=10)

    @model_validator(mode="after")
    def base_url_has_only_an_origin(self) -> "ReadOnlyHttpManifest":
        if self.base_url.username or self.base_url.password:
            raise ValueError("base_url must not contain credentials")
        if self.base_url.path not in {None, "/"} or self.base_url.query or self.base_url.fragment:
            raise ValueError("base_url must contain only scheme and authority")
        if self.base_url.scheme not in {"http", "https"}:
            raise ValueError("base_url must use HTTP or HTTPS")
        return self


class ExpensePolicy(_FrozenModel):
    category: str = Field(min_length=1, max_length=100)
    gl_code: str = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")
    cost_center: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    approval_required: bool
    approval_route: Literal["auto", "manager"]
    justification_threshold: float = Field(ge=0)


class DraftArtifact(_FrozenModel):
    request: CapabilityRequest
    directory: Path
    manifest: ReadOnlyHttpManifest
    file_sha256: dict[Literal["manifest.toml", "server.py"], str]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
