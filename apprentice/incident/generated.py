"""Validated, deterministic worlds authored as data by Terra.

The language model may invent the situation, but it never mutates a running
world.  A :class:`GeneratedScenarioSpec` is validated once and fingerprinted;
the runtime then applies only the causal rules contained in that immutable
spec.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from itertools import pairwise
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GeneratedScenarioError(ValueError):
    """Raised when a generated scenario or learner action is invalid."""


class GeneratedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SituationFact(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    statement: str = Field(min_length=1, max_length=600)
    source: str = Field(min_length=1, max_length=160)
    known_at_start: bool


class TimelineMoment(GeneratedModel):
    minutes_before_start: int = Field(ge=0, le=10080)
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=600)


class GeneratedMetric(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=40)
    initial: float
    minimum: float
    maximum: float
    precision: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def validate_range(self) -> GeneratedMetric:
        if self.minimum >= self.maximum:
            raise ValueError("metric minimum must be less than maximum")
        if not self.minimum <= self.initial <= self.maximum:
            raise ValueError("metric initial value must be within its range")
        return self


class GeneratedArtifact(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=1_500)
    visible_at_start: bool


class MetricEffect(GeneratedModel):
    metric_id: str = Field(min_length=1, max_length=80)
    operation: Literal["add", "set"]
    value: float


class GeneratedAction(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=500)
    risk: Literal["low", "medium", "high"]
    prerequisites: tuple[str, ...] = Field(max_length=8)
    metric_effects: tuple[MetricEffect, ...] = Field(max_length=12)
    reveals_artifacts: tuple[str, ...] = Field(max_length=8)
    advances_minutes: int = Field(ge=1, le=240)


class TimedEscalation(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    at_minute: int = Field(ge=1, le=1440)
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=700)
    metric_effects: tuple[MetricEffect, ...] = Field(max_length=12)
    reveals_artifacts: tuple[str, ...] = Field(max_length=8)
    terminal: bool


class SuccessRequirement(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = Field(min_length=1, max_length=300)
    kind: Literal[
        "action_completed",
        "artifact_revealed",
        "metric_at_most",
        "metric_at_least",
    ]
    ref: str = Field(min_length=1, max_length=80)
    threshold: float | None

    @model_validator(mode="after")
    def validate_threshold(self) -> SuccessRequirement:
        metric_kind = self.kind in {"metric_at_most", "metric_at_least"}
        if metric_kind != (self.threshold is not None):
            raise ValueError("only metric requirements must include a threshold")
        return self


class RubricEvidence(GeneratedModel):
    source: Literal["fact", "artifact", "metric", "action", "event"]
    ref: str = Field(min_length=1, max_length=80)


class GeneratedRubricCriterion(GeneratedModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    weight: int = Field(ge=1, le=10)
    evidence: tuple[RubricEvidence, ...] = Field(min_length=1, max_length=8)


class GeneratedScenarioSpec(GeneratedModel):
    """Complete model-authored contract for one novel simulation."""

    scenario_id: str = Field(min_length=8, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    generation_nonce: str = Field(min_length=8, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    learner_role: str = Field(min_length=1, max_length=200)
    setting: str = Field(min_length=1, max_length=500)
    core_challenge: str = Field(min_length=20, max_length=700)
    failure_mechanism: str = Field(min_length=20, max_length=700)
    decision_tradeoff: str = Field(min_length=20, max_length=700)
    briefing: str = Field(min_length=1, max_length=2_000)
    first_decision: str = Field(min_length=1, max_length=500)
    facts: tuple[SituationFact, ...] = Field(min_length=2, max_length=16)
    timeline: tuple[TimelineMoment, ...] = Field(min_length=2, max_length=8)
    metrics: tuple[GeneratedMetric, ...] = Field(min_length=1, max_length=12)
    artifacts: tuple[GeneratedArtifact, ...] = Field(min_length=1, max_length=16)
    actions: tuple[GeneratedAction, ...] = Field(min_length=2, max_length=16)
    timed_escalations: tuple[TimedEscalation, ...] = Field(min_length=1, max_length=8)
    success_requirements: tuple[SuccessRequirement, ...] = Field(min_length=1, max_length=10)
    rubric: tuple[GeneratedRubricCriterion, ...] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_semantics(self) -> GeneratedScenarioSpec:
        facts = _unique_by_id(self.facts, "facts")
        metrics = _unique_by_id(self.metrics, "metrics")
        artifacts = _unique_by_id(self.artifacts, "artifacts")
        actions = _unique_by_id(self.actions, "actions")
        events = _unique_by_id(self.timed_escalations, "timed escalations")
        _unique_by_id(self.success_requirements, "success requirements")
        _unique_by_id(self.rubric, "rubric criteria")

        if len({moment.minutes_before_start for moment in self.timeline}) != len(self.timeline):
            raise ValueError("timeline moments must have distinct times")
        if tuple(self.timeline) != tuple(
            sorted(self.timeline, key=lambda moment: moment.minutes_before_start, reverse=True)
        ):
            raise ValueError("timeline must run from oldest to most recent")
        if not any(fact.known_at_start for fact in self.facts):
            raise ValueError("at least one fact must be known at the start")

        for action in self.actions:
            if len(set(action.prerequisites)) != len(action.prerequisites):
                raise ValueError(f"action {action.id} repeats a prerequisite")
            unknown = set(action.prerequisites) - actions.keys()
            if unknown:
                raise ValueError(f"action {action.id} has unknown prerequisites: {sorted(unknown)}")
            if action.id in action.prerequisites:
                raise ValueError(f"action {action.id} cannot require itself")
            _validate_effects(action.metric_effects, metrics, f"action {action.id}")
            _validate_refs(action.reveals_artifacts, artifacts, f"action {action.id}")
        _validate_acyclic(actions)

        for event in self.timed_escalations:
            _validate_effects(event.metric_effects, metrics, f"event {event.id}")
            _validate_refs(event.reveals_artifacts, artifacts, f"event {event.id}")
        if not any(event.terminal for event in self.timed_escalations):
            raise ValueError("at least one timed escalation must be terminal")

        revealable = {item.id for item in self.artifacts if item.visible_at_start}
        revealable.update(ref for action in self.actions for ref in action.reveals_artifacts)
        revealable.update(
            ref for event in self.timed_escalations for ref in event.reveals_artifacts
        )
        for requirement in self.success_requirements:
            refs = actions if requirement.kind == "action_completed" else artifacts
            if requirement.kind.startswith("metric_"):
                refs = metrics
            if requirement.ref not in refs:
                raise ValueError(
                    f"success requirement {requirement.id} has unknown ref: {requirement.ref}"
                )
            if requirement.kind == "artifact_revealed" and requirement.ref not in revealable:
                raise ValueError(f"required artifact cannot be revealed: {requirement.ref}")
            if requirement.threshold is not None:
                metric = metrics[requirement.ref]
                if not metric.minimum <= requirement.threshold <= metric.maximum:
                    raise ValueError(
                        f"success threshold for {requirement.ref} is outside its metric range"
                    )

        evidence_maps = {
            "fact": facts,
            "artifact": artifacts,
            "metric": metrics,
            "action": actions,
            "event": events,
        }
        for criterion in self.rubric:
            for evidence in criterion.evidence:
                if evidence.ref not in evidence_maps[evidence.source]:
                    raise ValueError(
                        f"rubric {criterion.id} has unknown {evidence.source} ref: {evidence.ref}"
                    )
        if not any(not action.prerequisites for action in self.actions):
            raise ValueError("at least one action must be enabled initially")
        if not _has_recovery_path(self):
            raise ValueError(
                "generated world has no reachable recovery path through its action graph"
            )
        return self

    @property
    def fingerprint(self) -> str:
        """Hash causal semantics, deliberately excluding cosmetic framing.

        Scenario ids, nonce, title, learner-role wording, briefing and display
        labels do not participate.  A caller can therefore reject the same
        world even when Terra gives it a fresh id or rewrites its title.
        """
        metric_refs = {
            metric.id: f"metric:{index}"
            for index, metric in enumerate(
                sorted(
                    self.metrics,
                    key=lambda item: (
                        _normalize(item.unit),
                        item.initial,
                        item.minimum,
                        item.maximum,
                        item.precision,
                        _normalize(item.label),
                    ),
                )
            )
        }
        artifact_refs = {
            artifact.id: f"artifact:{index}"
            for index, artifact in enumerate(
                sorted(
                    self.artifacts,
                    key=lambda item: (
                        _normalize(item.kind),
                        _normalize(item.content),
                        item.visible_at_start,
                    ),
                )
            )
        }
        action_signatures = {
            action.id: _action_signature(action, metric_refs, artifact_refs)
            for action in self.actions
        }
        projection = {
            "core_challenge": _normalize(self.core_challenge),
            "failure_mechanism": _normalize(self.failure_mechanism),
            "decision_tradeoff": _normalize(self.decision_tradeoff),
            "metrics": sorted(
                [
                    {
                        "ref": metric_refs[metric.id],
                        "unit": _normalize(metric.unit),
                        "initial": metric.initial,
                        "minimum": metric.minimum,
                        "maximum": metric.maximum,
                        "precision": metric.precision,
                    }
                    for metric in self.metrics
                ],
                key=lambda item: item["ref"],
            ),
            "artifacts": sorted(
                [
                    {
                        "ref": artifact_refs[artifact.id],
                        "kind": _normalize(artifact.kind),
                        "content": _normalize(artifact.content),
                        "visible": artifact.visible_at_start,
                    }
                    for artifact in self.artifacts
                ],
                key=lambda item: item["ref"],
            ),
            "actions": sorted(
                [
                    {
                        "signature": action_signatures[action.id],
                        "prerequisites": sorted(
                            action_signatures[ref] for ref in action.prerequisites
                        ),
                    }
                    for action in self.actions
                ],
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
            "events": sorted(
                [
                    {
                        "at_minute": event.at_minute,
                        "detail": _normalize(event.detail),
                        "effects": _effect_projection(event.metric_effects, metric_refs),
                        "reveals": sorted(
                            artifact_refs[ref] for ref in event.reveals_artifacts
                        ),
                        "terminal": event.terminal,
                    }
                    for event in self.timed_escalations
                ],
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        }
        canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def novelty_text(self) -> str:
        return " ".join(
            _normalize(value)
            for value in (self.core_challenge, self.failure_mechanism, self.decision_tradeoff)
        )


def novelty_similarity(first: GeneratedScenarioSpec, second: GeneratedScenarioSpec) -> float:
    """Return token/bigram Dice similarity for inexpensive deduplication."""
    first_tokens = _novelty_tokens(first.novelty_text)
    second_tokens = _novelty_tokens(second.novelty_text)
    if not first_tokens and not second_tokens:
        return 1.0
    return 2 * len(first_tokens & second_tokens) / (len(first_tokens) + len(second_tokens))


def is_near_duplicate(
    first: GeneratedScenarioSpec,
    second: GeneratedScenarioSpec,
    *,
    threshold: float = 0.72,
) -> bool:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("near-duplicate threshold must be between zero and one")
    return first.fingerprint == second.fingerprint or novelty_similarity(first, second) >= threshold


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _novelty_tokens(value: str) -> set[str]:
    words = value.split()
    tokens = set(words)
    tokens.update(f"{left} {right}" for left, right in pairwise(words))
    return tokens


def _effect_projection(
    effects: tuple[MetricEffect, ...], metric_refs: dict[str, str]
) -> list[dict[str, str | float]]:
    return sorted(
        (
            {
                "metric": metric_refs[effect.metric_id],
                "operation": effect.operation,
                "value": effect.value,
            }
            for effect in effects
        ),
        key=lambda item: str(item["metric"]),
    )


def _action_signature(
    action: GeneratedAction,
    metric_refs: dict[str, str],
    artifact_refs: dict[str, str],
) -> str:
    content = {
        "description": _normalize(action.description),
        "risk": action.risk,
        "effects": _effect_projection(action.metric_effects, metric_refs),
        "reveals": sorted(artifact_refs[ref] for ref in action.reveals_artifacts),
        "advances_minutes": action.advances_minutes,
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _has_recovery_path(spec: GeneratedScenarioSpec) -> bool:
    """Explore every bounded one-shot action path until recovery or escalation."""
    metric_defs = {metric.id: metric for metric in spec.metrics}
    event_defs = sorted(spec.timed_escalations, key=lambda item: (item.at_minute, item.id))
    initial_metrics = tuple((metric.id, metric.initial) for metric in spec.metrics)
    initial_artifacts = frozenset(
        artifact.id for artifact in spec.artifacts if artifact.visible_at_start
    )
    stack = [(frozenset(), 0, initial_metrics, initial_artifacts, frozenset())]
    visited: set[tuple[object, ...]] = set()
    max_states = 100_000

    while stack:
        completed, sim_time, metric_values, revealed, fired = stack.pop()
        state_key = (completed, sim_time, metric_values, revealed, fired)
        if state_key in visited:
            continue
        visited.add(state_key)
        if len(visited) > max_states:
            return False
        metrics = dict(metric_values)
        if _requirements_met(spec, completed, revealed, metrics):
            return True

        for action in spec.actions:
            if action.id in completed or not set(action.prerequisites) <= completed:
                continue
            next_completed = completed | {action.id}
            next_metrics = dict(metrics)
            _apply_projected_effects(next_metrics, metric_defs, action.metric_effects)
            next_revealed = revealed | set(action.reveals_artifacts)
            next_time = sim_time + action.advances_minutes
            next_fired = set(fired)
            terminal = False
            for event in event_defs:
                if (
                    event.id in next_fired
                    or not sim_time < event.at_minute <= next_time
                ):
                    continue
                _apply_projected_effects(next_metrics, metric_defs, event.metric_effects)
                next_revealed |= set(event.reveals_artifacts)
                next_fired.add(event.id)
                if event.terminal:
                    terminal = True
                    break
            if not terminal:
                stack.append(
                    (
                        frozenset(next_completed),
                        next_time,
                        tuple((metric.id, next_metrics[metric.id]) for metric in spec.metrics),
                        frozenset(next_revealed),
                        frozenset(next_fired),
                    )
                )
    return False


def _apply_projected_effects(
    metrics: dict[str, float],
    definitions: dict[str, GeneratedMetric],
    effects: tuple[MetricEffect, ...],
) -> None:
    for effect in effects:
        definition = definitions[effect.metric_id]
        previous = metrics[effect.metric_id]
        candidate = effect.value if effect.operation == "set" else previous + effect.value
        updated = min(definition.maximum, max(definition.minimum, candidate))
        metrics[effect.metric_id] = round(updated, definition.precision)


def _requirements_met(
    spec: GeneratedScenarioSpec,
    completed: frozenset[str],
    revealed: frozenset[str],
    metrics: dict[str, float],
) -> bool:
    for requirement in spec.success_requirements:
        if requirement.kind == "action_completed" and requirement.ref not in completed:
            return False
        if requirement.kind == "artifact_revealed" and requirement.ref not in revealed:
            return False
        if (
            requirement.kind == "metric_at_most"
            and metrics[requirement.ref] > float(requirement.threshold)
        ):
            return False
        if (
            requirement.kind == "metric_at_least"
            and metrics[requirement.ref] < float(requirement.threshold)
        ):
            return False
    return True


def _unique_by_id(items: tuple[GeneratedModel, ...], label: str) -> dict[str, GeneratedModel]:
    result: dict[str, GeneratedModel] = {}
    for item in items:
        item_id = str(item.id)
        if item_id in result:
            raise ValueError(f"{label} contain duplicate id: {item_id}")
        result[item_id] = item
    return result


def _validate_refs(refs: tuple[str, ...], known: dict[str, GeneratedModel], owner: str) -> None:
    if len(set(refs)) != len(refs):
        raise ValueError(f"{owner} repeats an artifact ref")
    unknown = set(refs) - known.keys()
    if unknown:
        raise ValueError(f"{owner} has unknown artifact refs: {sorted(unknown)}")


def _validate_effects(
    effects: tuple[MetricEffect, ...], metrics: dict[str, GeneratedModel], owner: str
) -> None:
    refs = [effect.metric_id for effect in effects]
    if len(set(refs)) != len(refs):
        raise ValueError(f"{owner} changes the same metric more than once")
    unknown = set(refs) - metrics.keys()
    if unknown:
        raise ValueError(f"{owner} has unknown metric refs: {sorted(unknown)}")
    for effect in effects:
        metric = metrics[effect.metric_id]
        if effect.operation == "set" and not metric.minimum <= effect.value <= metric.maximum:
            raise ValueError(f"{owner} sets {effect.metric_id} outside its metric range")


def _validate_acyclic(actions: dict[str, GeneratedModel]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(action_id: str) -> None:
        if action_id in visiting:
            raise ValueError("action prerequisites must be acyclic")
        if action_id in visited:
            return
        visiting.add(action_id)
        action = actions[action_id]
        for prerequisite in action.prerequisites:
            visit(prerequisite)
        visiting.remove(action_id)
        visited.add(action_id)

    for action_id in actions:
        visit(action_id)


class GeneratedScenarioRuntime:
    """In-memory deterministic interpreter for validated generated specs."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, object]] = {}

    def create(self, spec: GeneratedScenarioSpec) -> dict[str, object]:
        # Revalidate a serialized copy so callers cannot bypass validation via
        # model_construct, and retain an independent immutable instance.
        canonical_spec = GeneratedScenarioSpec.model_validate(spec.model_dump(mode="json"))
        run_id = str(uuid4())
        state: dict[str, object] = {
            "id": run_id,
            "spec": canonical_spec,
            "scenario_id": canonical_spec.scenario_id,
            "scenario_fingerprint": canonical_spec.fingerprint,
            "sim_time": 0,
            "metrics": {metric.id: metric.initial for metric in canonical_spec.metrics},
            "completed_actions": [],
            "revealed_artifacts": [
                artifact.id for artifact in canonical_spec.artifacts if artifact.visible_at_start
            ],
            "fired_events": [],
            "events": [
                {
                    "index": 0,
                    "event_index": 0,
                    "time": 0,
                    "type": "scenario_started",
                    "ref": canonical_spec.scenario_id,
                    "title": canonical_spec.title,
                    "detail": canonical_spec.briefing,
                    "metric_delta": {},
                    "delta": {},
                    "rule": "generated.scenario.start",
                }
            ],
            "outcome": "active",
            "terminal_reason": None,
        }
        self._runs[run_id] = state
        return self._snapshot(state)

    def restore(
        self, spec: GeneratedScenarioSpec, snapshot: dict[str, object]
    ) -> dict[str, object]:
        """Hydrate a run from a previously returned snapshot.

        Derived display fields are ignored and rebuilt.  Canonical state is
        validated against the immutable spec before it becomes executable.
        """
        canonical_spec = GeneratedScenarioSpec.model_validate(spec.model_dump(mode="json"))
        run_id = str(snapshot.get("id", ""))
        if not run_id:
            raise GeneratedScenarioError("restored simulation must include an id")
        if run_id in self._runs:
            raise GeneratedScenarioError(f"generated simulation already exists: {run_id}")
        if snapshot.get("scenario_id") != canonical_spec.scenario_id:
            raise GeneratedScenarioError("restored simulation scenario id does not match spec")
        if snapshot.get("scenario_fingerprint") != canonical_spec.fingerprint:
            raise GeneratedScenarioError("restored simulation fingerprint does not match spec")

        metric_defs = {metric.id: metric for metric in canonical_spec.metrics}
        raw_metrics = snapshot.get("metrics")
        if not isinstance(raw_metrics, dict) or set(raw_metrics) != set(metric_defs):
            raise GeneratedScenarioError("restored simulation metrics do not match spec")
        metrics: dict[str, float] = {}
        for metric_id, definition in metric_defs.items():
            value = raw_metrics[metric_id]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise GeneratedScenarioError(f"restored metric is not numeric: {metric_id}")
            if not definition.minimum <= float(value) <= definition.maximum:
                raise GeneratedScenarioError(f"restored metric is outside its range: {metric_id}")
            metrics[metric_id] = float(value)

        completed = _validated_string_list(snapshot, "completed_actions")
        action_defs = {action.id: action for action in canonical_spec.actions}
        if not set(completed) <= action_defs.keys():
            raise GeneratedScenarioError("restored simulation has unknown completed actions")
        revealed = _validated_string_list(snapshot, "revealed_artifacts")
        artifact_ids = {artifact.id for artifact in canonical_spec.artifacts}
        initially_visible = {
            artifact.id for artifact in canonical_spec.artifacts if artifact.visible_at_start
        }
        if not set(revealed) <= artifact_ids or not initially_visible <= set(revealed):
            raise GeneratedScenarioError("restored simulation has invalid revealed artifacts")

        fired = _validated_string_list(snapshot, "fired_events")
        event_defs = {event.id: event for event in canonical_spec.timed_escalations}
        if not set(fired) <= event_defs.keys():
            raise GeneratedScenarioError("restored simulation has unknown fired events")
        sim_time = snapshot.get("sim_time")
        if isinstance(sim_time, bool) or not isinstance(sim_time, int) or sim_time < 0:
            raise GeneratedScenarioError("restored simulation has invalid simulated time")
        if any(event_defs[event_id].at_minute > sim_time for event_id in fired):
            raise GeneratedScenarioError("restored simulation fired an event before its time")

        outcome = snapshot.get("outcome")
        if outcome not in {"active", "recovered", "terminal_escalation"}:
            raise GeneratedScenarioError("restored simulation has invalid outcome")
        events = snapshot.get("events")
        if not isinstance(events, list) or not events:
            raise GeneratedScenarioError("restored simulation must include its event history")
        state: dict[str, object] = {
            "id": run_id,
            "spec": canonical_spec,
            "scenario_id": canonical_spec.scenario_id,
            "scenario_fingerprint": canonical_spec.fingerprint,
            "sim_time": sim_time,
            "metrics": metrics,
            "completed_actions": completed,
            "revealed_artifacts": revealed,
            "fired_events": fired,
            "events": deepcopy(events),
            "outcome": outcome,
            "terminal_reason": snapshot.get("terminal_reason"),
        }
        if outcome == "recovered" and not self._success(state, canonical_spec):
            raise GeneratedScenarioError("restored recovered simulation does not meet success")
        self._runs[run_id] = state
        return self._snapshot(state)

    def snapshot(self, run_id: str) -> dict[str, object]:
        return self._snapshot(self._get(run_id))

    def apply(
        self, run_id: str, action_id: str, *, allow_out_of_order: bool = False
    ) -> dict[str, object]:
        state = self._get(run_id)
        if state["outcome"] != "active":
            raise GeneratedScenarioError("the simulation has already ended")
        spec = state["spec"]
        assert isinstance(spec, GeneratedScenarioSpec)
        actions = {action.id: action for action in spec.actions}
        action = actions.get(action_id)
        if action is None:
            raise GeneratedScenarioError(f"unknown generated action: {action_id}")
        completed = set(state["completed_actions"])
        if action_id in completed:
            raise GeneratedScenarioError(f"action has already been completed: {action_id}")
        missing = set(action.prerequisites) - completed
        if missing and not allow_out_of_order:
            raise GeneratedScenarioError(
                f"action {action_id} is missing prerequisites: {sorted(missing)}"
            )

        before_time = int(state["sim_time"])
        delta = self._apply_effects(state, spec, action.metric_effects)
        state["completed_actions"].append(action_id)
        self._reveal(state, action.reveals_artifacts)
        state["sim_time"] = before_time + action.advances_minutes
        self._append_event(
            state,
            event_type="action",
            ref=action.id,
            title=action.label,
            detail=action.description,
            metric_delta=delta,
            rule=f"generated.action.{action.id}",
        )
        self._fire_due_events(state, spec, before_time)
        if state["outcome"] == "active" and self._success(state, spec):
            state["outcome"] = "recovered"
            self._append_event(
                state,
                event_type="outcome",
                ref="success",
                title="Success requirements met",
                detail="The learner satisfied every requirement in the generated world.",
                metric_delta={},
                rule="generated.outcome.success",
            )
        return self._snapshot(state)

    def _fire_due_events(
        self, state: dict[str, object], spec: GeneratedScenarioSpec, before_time: int
    ) -> None:
        fired = set(state["fired_events"])
        now = int(state["sim_time"])
        for event in sorted(spec.timed_escalations, key=lambda item: (item.at_minute, item.id)):
            if event.id in fired or not before_time < event.at_minute <= now:
                continue
            delta = self._apply_effects(state, spec, event.metric_effects)
            self._reveal(state, event.reveals_artifacts)
            state["fired_events"].append(event.id)
            self._append_event(
                state,
                event_type="timed_escalation",
                ref=event.id,
                title=event.title,
                detail=event.detail,
                metric_delta=delta,
                rule=f"generated.clock.{event.id}",
                event_time=event.at_minute,
            )
            if event.terminal:
                state["outcome"] = "terminal_escalation"
                state["terminal_reason"] = event.detail
                break

    @staticmethod
    def _apply_effects(
        state: dict[str, object],
        spec: GeneratedScenarioSpec,
        effects: tuple[MetricEffect, ...],
    ) -> dict[str, float]:
        metrics = state["metrics"]
        definitions = {metric.id: metric for metric in spec.metrics}
        delta: dict[str, float] = {}
        for effect in effects:
            definition = definitions[effect.metric_id]
            previous = float(metrics[effect.metric_id])
            candidate = effect.value if effect.operation == "set" else previous + effect.value
            updated = min(definition.maximum, max(definition.minimum, candidate))
            updated = round(updated, definition.precision)
            metrics[effect.metric_id] = updated
            delta[effect.metric_id] = round(updated - previous, definition.precision)
        return delta

    @staticmethod
    def _reveal(state: dict[str, object], artifact_ids: tuple[str, ...]) -> None:
        revealed = state["revealed_artifacts"]
        for artifact_id in artifact_ids:
            if artifact_id not in revealed:
                revealed.append(artifact_id)

    @staticmethod
    def _success(state: dict[str, object], spec: GeneratedScenarioSpec) -> bool:
        completed = set(state["completed_actions"])
        revealed = set(state["revealed_artifacts"])
        metrics = state["metrics"]
        for requirement in spec.success_requirements:
            if requirement.kind == "action_completed" and requirement.ref not in completed:
                return False
            if requirement.kind == "artifact_revealed" and requirement.ref not in revealed:
                return False
            if (
                requirement.kind == "metric_at_most"
                and float(metrics[requirement.ref]) > float(requirement.threshold)
            ):
                return False
            if (
                requirement.kind == "metric_at_least"
                and float(metrics[requirement.ref]) < float(requirement.threshold)
            ):
                return False
        return True

    def _snapshot(self, state: dict[str, object]) -> dict[str, object]:
        spec = state["spec"]
        assert isinstance(spec, GeneratedScenarioSpec)
        completed = set(state["completed_actions"])
        artifact_map = {artifact.id: artifact for artifact in spec.artifacts}
        public = {key: value for key, value in state.items() if key != "spec"}
        public["scenario"] = spec.scenario_id
        public["generated_spec"] = spec.model_dump(mode="json")
        public["artifacts"] = [
            artifact_map[artifact_id].model_dump(mode="json")
            for artifact_id in state["revealed_artifacts"]
        ]
        public["available_actions"] = [
            {
                "id": action.id,
                "kind": action.id,
                "label": action.label,
                "description": action.description,
                "risk": action.risk,
                "enabled": (
                    state["outcome"] == "active"
                    and action.id not in completed
                    and set(action.prerequisites) <= completed
                ),
            }
            for action in spec.actions
        ]
        public["success_requirements_met"] = self._success(state, spec)
        public["completed"] = state["outcome"] == "recovered"
        public["terminal"] = state["outcome"] == "terminal_escalation"
        return deepcopy(public)

    def _get(self, run_id: str) -> dict[str, object]:
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise GeneratedScenarioError(f"unknown generated simulation: {run_id}") from error

    @staticmethod
    def _append_event(
        state: dict[str, object],
        *,
        event_type: str,
        ref: str,
        title: str,
        detail: str,
        metric_delta: dict[str, float],
        rule: str,
        event_time: int | None = None,
    ) -> None:
        events = state["events"]
        events.append(
            {
                "index": len(events),
                "event_index": len(events),
                "time": int(state["sim_time"]) if event_time is None else event_time,
                "type": event_type,
                "ref": ref,
                "title": title,
                "detail": detail,
                "metric_delta": metric_delta,
                "delta": metric_delta,
                "rule": rule,
            }
        )


def _validated_string_list(snapshot: dict[str, object], key: str) -> list[str]:
    value = snapshot.get(key)
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise GeneratedScenarioError(f"restored simulation has invalid {key}")
    return list(value)
