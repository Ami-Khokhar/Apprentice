"""Deterministic alignment and variance detection across training demonstrations.

Induction must never guess which recorded values are meaningful inputs versus
incidental noise. This module inspects the *structure* of aligned training
demonstrations (matched by anchor identity, not by filename or order) and
produces a small, JSON-serializable evidence report. That report is embedded
in the GPT-5.6 induction prompt; the model still decides what becomes an
input or a decision point, but it does so against facts we computed, not
values it invents.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_FIELD_ACTIONS = frozenset({"input", "upload"})


class InsufficientDemonstrationsError(ValueError):
    """Raised when induction is attempted from fewer than two demonstrations."""


@dataclass(frozen=True)
class _AnchorKey:
    css: str
    role: str
    name: str | None


def _anchor_key(anchor: Mapping[str, Any]) -> _AnchorKey:
    return _AnchorKey(css=anchor["css"], role=anchor["role"], name=anchor.get("name"))


def _rendered_value(step: Mapping[str, Any]) -> str | None:
    """Return the semantic, human-legible value a step contributed, if any."""

    if step["action"] == "input":
        return step["value"]
    if step["action"] == "upload":
        files = step["value"]["files"]
        return files[0]["filename"] if files else None
    return None


@dataclass
class FieldEvidence:
    """What a single anchored field looked like across aligned demonstrations."""

    anchor: dict[str, Any]
    action: str
    values_by_demonstration: dict[str, str | None] = field(default_factory=dict)

    @property
    def present_values(self) -> list[str]:
        return [value for value in self.values_by_demonstration.values() if value is not None]

    @property
    def always_present(self) -> bool:
        return all(value is not None for value in self.values_by_demonstration.values())

    @property
    def conditional(self) -> bool:
        """Present in some demonstrations and absent in others (a policy branch)."""

        present = any(value is not None for value in self.values_by_demonstration.values())
        absent = any(value is None for value in self.values_by_demonstration.values())
        return present and absent

    @property
    def varies(self) -> bool:
        """Distinct non-empty values were demonstrated (a genuine input, not a branch flag)."""

        return len(set(self.present_values)) > 1

    def to_evidence(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor,
            "action": self.action,
            "values_by_demonstration": dict(self.values_by_demonstration),
            "varies": self.varies,
            "always_present": self.always_present,
            "conditional": self.conditional,
        }


def align_input_fields(
    training_demonstrations: Mapping[str, Mapping[str, Any]],
) -> list[FieldEvidence]:
    """Align input/upload steps across demonstrations by anchor identity.

    Fields are returned in first-seen order so evidence is deterministic
    regardless of dict ordering upstream.
    """

    demonstration_names = list(training_demonstrations)
    order: list[_AnchorKey] = []
    by_anchor: dict[_AnchorKey, FieldEvidence] = {}

    for demo_name, artifact in training_demonstrations.items():
        for step in artifact["steps"]:
            if step["action"] not in _FIELD_ACTIONS:
                continue
            key = _anchor_key(step["anchor"])
            if key not in by_anchor:
                order.append(key)
                by_anchor[key] = FieldEvidence(
                    anchor=dict(step["anchor"]),
                    action=step["action"],
                    values_by_demonstration=dict.fromkeys(demonstration_names),
                )
            by_anchor[key].values_by_demonstration[demo_name] = _rendered_value(step)

    return [by_anchor[key] for key in order]


def build_variance_evidence(
    training_demonstrations: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the deterministic variance report to embed in the induction prompt."""

    if len(training_demonstrations) < 2:
        raise InsufficientDemonstrationsError(
            "Induction requires at least two training demonstrations"
        )
    return [field.to_evidence() for field in align_input_fields(training_demonstrations)]
