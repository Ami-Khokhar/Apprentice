"""GPT-5.6 playbook induction from aligned training demonstrations.

Deterministic code (``apprentice.induction.normalize``) computes which
recorded values vary across training demonstrations; GPT-5.6, run through a
single OpenAI Agents SDK agent with a typed ``output_type``, decides which of
those variances become named inputs and which become policy decision
points, citing the demonstrations that support each decision. This module
validates the model's answer against the same deterministic evidence before
it is ever eligible to become a human-reviewed playbook.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from agents import Agent, Runner
from pydantic import BaseModel, Field, model_validator

from apprentice.induction.normalize import InsufficientDemonstrationsError, build_variance_evidence
from apprentice.models import Demonstration, DemonstrationRole
from apprentice.providers import provider_model, provider_model_settings

__all__ = [
    "Anchor",
    "ArtifactLoader",
    "DecisionPoint",
    "InductionRunner",
    "InductionValidationError",
    "InputSpec",
    "Playbook",
    "PlaybookDraft",
    "PlaybookStep",
    "SuccessCriteria",
    "build_induction_agent",
    "induce_playbook",
    "select_training_artifacts",
]


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class InductionValidationError(ValueError):
    """Raised when the model's playbook contradicts the deterministic evidence."""


class Anchor(BaseModel):
    """A semantic locator for a page element: role, accessible name, CSS fallback."""

    css: str
    role: str
    name: str | None = None


class InputSpec(BaseModel):
    """A named, human-reviewable input the induced procedure requires."""

    name: str
    description: str
    anchor: Anchor
    required: bool = True


class SuccessCriteria(BaseModel):
    """How the procedure's final page state is judged to have succeeded."""

    status_code: int
    page_contains: str


# The plan's binding contract (§6.1) defines one legal effect per action.
# Anything else is an "unknown action/effect combination" and fails
# validation; in particular a commit can never be downgraded to click, and a
# commit action can never be relabeled prepare/observe.
_ALLOWED_EFFECT: dict[str, str] = {
    "navigate": "observe",
    "fill": "prepare",
    "upload": "prepare",
    "click": "prepare",
    "commit": "commit",
}

_TEMPLATE_REFERENCE = re.compile(r"\{([^{}]+)\}")


class PlaybookStep(BaseModel):
    """One canonical step of the induced procedure (plan contract §6.1).

    Recorded artifacts use the raw recorder vocabulary (``input``,
    ``submit``, ``page_state``); induction maps those to this contract
    vocabulary deterministically: ``input`` becomes ``fill``, ``submit``
    becomes ``commit``, and ``page_state`` observations are folded into the
    prompt evidence rather than emitted as steps. Only a ``commit`` action
    may carry the ``commit`` effect: it is the single step allowed to
    trigger the recorded mutation.
    """

    intent: str
    action: Literal["navigate", "fill", "upload", "click", "commit"]
    anchor: Anchor
    value_template: str | None
    effect: Literal["observe", "prepare", "commit"]

    @model_validator(mode="after")
    def _known_action_effect_combination(self) -> PlaybookStep:
        if _ALLOWED_EFFECT[self.action] != self.effect:
            raise ValueError(
                f"unknown action/effect combination: {self.action}/{self.effect}"
            )
        return self


class DecisionPoint(BaseModel):
    """A branching policy the model inferred from demonstrated variance.

    ``supporting_demonstrations`` must name at least one demonstration the
    model was actually shown; induction rejects fabricated citations.
    """

    condition: str
    input_ref: str
    activates_input: str
    supporting_demonstrations: tuple[str, ...] = Field(min_length=1)
    description: str = ""


class Playbook(BaseModel):
    """The induced, typed procedure GPT-5.6 produces for human review.

    Field set and vocabularies follow the plan's binding contract (§6.1).
    """

    task: str
    goal: str
    inputs: list[InputSpec]
    preconditions: list[str]
    steps: list[PlaybookStep]
    decision_points: list[DecisionPoint]
    success_criteria: SuccessCriteria
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def _each_declared_input_is_used(self) -> Playbook:
        """Plan rule: each declared input is used by a value template or decision point."""

        referenced: set[str] = set()
        for step in self.steps:
            if step.value_template is not None:
                referenced.update(_TEMPLATE_REFERENCE.findall(step.value_template))
        for decision in self.decision_points:
            referenced.add(decision.input_ref)
            referenced.add(decision.activates_input)
        unused = [spec.name for spec in self.inputs if spec.name not in referenced]
        if unused:
            raise ValueError(
                "declared inputs are not used by any value template or "
                f"decision point: {unused}"
            )
        return self


# The agent's structured output type. Named separately per spec even though it
# is currently identical to the reviewed ``Playbook`` shape: the model's raw
# answer is a draft until deterministic validation and human review accept it.
PlaybookDraft = Playbook

InductionRunner = Callable[[Agent[Any], str], Playbook]
ArtifactLoader = Callable[[str], Mapping[str, Any]]


def select_training_artifacts(
    demonstrations: Iterable[Demonstration],
    *,
    loader: ArtifactLoader,
) -> dict[str, Mapping[str, Any]]:
    """Select only ``role=training`` demonstrations and load their artifacts.

    Selection inspects ``demonstration.role`` alone. It never infers a
    demonstration's role from ``artifact_ref`` naming, so a held-out trace
    cannot leak into induction just because a fixture or path happens to be
    named like a training example (and vice versa).
    """

    return {
        demonstration.id: loader(demonstration.artifact_ref)
        for demonstration in demonstrations
        if demonstration.role is DemonstrationRole.TRAINING
    }


def build_induction_agent() -> Agent[Any]:
    """Construct the single GPT-5.6 induction agent with a typed output."""

    return Agent(
        name="apprentice-playbook-induction",
        instructions=(
            "You induce a reusable browser playbook from aligned human "
            "demonstrations of the same task. You are given, for each "
            "recorded field, the exact values observed across demonstrations "
            "(pre-computed; do not invent values). Decide which fields "
            "become named inputs and which fields reveal a policy decision "
            "point (a field present in some demonstrations and absent in "
            "others, correlated with another input's value). For every "
            "decision point, cite by name every demonstration that supports "
            "it. Emit steps in the contract vocabulary: recorded 'input' "
            "events become action='fill', the recorded 'submit' becomes the "
            "single action='commit' step with effect='commit', and "
            "'page_state' observations are context, not steps. Effects are "
            "navigate=observe, fill/upload/click=prepare, commit=commit. "
            "Reference each input from a step's value_template as {name}. "
            "The commit step is the only step allowed to trigger the "
            "recorded mutation."
        ),
        model=provider_model(),
        model_settings=provider_model_settings(),
        output_type=PlaybookDraft,
    )


def _render_induction_prompt(
    training_demonstrations: Mapping[str, Mapping[str, Any]],
    evidence: list[dict[str, Any]],
) -> str:
    payload = {
        "tasks": {name: artifact["task"] for name, artifact in training_demonstrations.items()},
        "demonstration_names": list(training_demonstrations),
        "variance_evidence": evidence,
    }
    return json.dumps(payload, sort_keys=True)


def _default_runner(agent: Agent[Any], prompt: str) -> Playbook:
    result = Runner.run_sync(agent, prompt)
    output = result.final_output
    assert isinstance(output, Playbook)
    return output


def _mutation_recorded(training_demonstrations: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(
        entry["method"] in _MUTATING_METHODS
        for artifact in training_demonstrations.values()
        for entry in artifact.get("http_entries", [])
    )


def _complete_recorded_structure(
    playbook: Playbook,
    training_demonstrations: Mapping[str, Mapping[str, Any]],
) -> Playbook:
    """Restore factual navigation structure that needs no model judgment."""

    first_artifact = next(iter(training_demonstrations.values()))
    recorded_navigation = next(
        (step for step in first_artifact["steps"] if step["action"] == "navigate"),
        None,
    )
    if recorded_navigation is not None and not any(
        step.action == "navigate" for step in playbook.steps
    ):
        title = recorded_navigation["page_state"]["title"]
        navigation = PlaybookStep(
            intent=f"Open the {title.lower()} page",
            action="navigate",
            anchor=Anchor(css="main", role="document", name=title),
            value_template=None,
            effect="observe",
        )
        playbook = Playbook.model_validate(
            playbook.model_copy(update={"steps": [navigation, *playbook.steps]}).model_dump()
        )

    recorded_submit = next(
        (
            step
            for artifact in training_demonstrations.values()
            for step in artifact["steps"]
            if step["action"] == "submit"
        ),
        None,
    )
    steps = list(playbook.steps)
    if recorded_submit is not None:
        commit_anchor = Anchor.model_validate(recorded_submit["anchor"])
        steps = [
            step.model_copy(update={"anchor": commit_anchor})
            if step.action == "commit"
            else step
            for step in steps
        ]

    if recorded_navigation is not None:
        control_order = {
            (control["css"], control["role"], control["name"]): index
            for index, control in enumerate(recorded_navigation["page_state"]["controls"])
        }

        def order_key(step: PlaybookStep) -> tuple[int, int]:
            if step.action == "navigate":
                return (0, 0)
            if step.action == "commit":
                return (2, 0)
            anchor = (step.anchor.css, step.anchor.role, step.anchor.name)
            return (1, control_order.get(anchor, len(control_order)))

        steps.sort(key=order_key)

    mutations = [
        entry
        for artifact in training_demonstrations.values()
        for entry in artifact.get("http_entries", [])
        if entry["method"] in _MUTATING_METHODS
    ]
    final_states = [
        step["page_state"]
        for artifact in training_demonstrations.values()
        for step in artifact["steps"]
        if step["action"] == "page_state"
    ]
    success_criteria = playbook.success_criteria
    if mutations and final_states:
        success_criteria = SuccessCriteria(
            status_code=mutations[0]["response"]["status"],
            page_contains=final_states[-1]["title"],
        )

    return Playbook.model_validate(
        playbook.model_copy(
            update={"steps": steps, "success_criteria": success_criteria}
        ).model_dump()
    )


def _validate_against_evidence(
    playbook: Playbook,
    evidence: list[dict[str, Any]],
    demonstration_names: set[str],
    *,
    mutation_recorded: bool,
) -> None:
    if mutation_recorded and not any(step.effect == "commit" for step in playbook.steps):
        raise InductionValidationError(
            "a recorded mutating request requires at least one effect=commit step"
        )

    # Anchor identity is (css, role, name) — the same key normalization
    # aligns on — so an input anchored to the right element under a renamed
    # accessible name does not silently satisfy the evidence.
    input_anchors = {
        (spec.anchor.css, spec.anchor.role, spec.anchor.name) for spec in playbook.inputs
    }
    for entry in evidence:
        if not (entry["varies"] or entry["conditional"] or entry["action"] == "upload"):
            continue
        anchor = entry["anchor"]
        if (anchor["css"], anchor["role"], anchor.get("name")) not in input_anchors:
            raise InductionValidationError(
                "playbook omits an input for a field that varies across "
                f"training demonstrations: {anchor}"
            )

    input_names = {spec.name for spec in playbook.inputs}
    for decision in playbook.decision_points:
        unknown = set(decision.supporting_demonstrations) - demonstration_names
        if unknown:
            raise InductionValidationError(
                "decision point cites demonstrations that were never shown "
                f"to the model: {sorted(unknown)}"
            )
        if decision.input_ref not in input_names:
            raise InductionValidationError(
                f"decision point references an unknown input: {decision.input_ref!r}"
            )
        if decision.activates_input not in input_names:
            raise InductionValidationError(
                f"decision point activates an unknown input: {decision.activates_input!r}"
            )


def induce_playbook(
    training_demonstrations: Mapping[str, Mapping[str, Any]],
    *,
    runner: InductionRunner | None = None,
) -> Playbook:
    """Induce a typed, evidence-validated Playbook from training demonstrations.

    ``runner`` is injected in tests so no real model call is ever required to
    exercise induction. When omitted, the real Agents SDK runner is used,
    which requires a configured OpenAI API key.
    """

    if len(training_demonstrations) < 2:
        raise InsufficientDemonstrationsError(
            "Induction requires at least two training demonstrations"
        )
    evidence = build_variance_evidence(training_demonstrations)
    agent = build_induction_agent()
    prompt = _render_induction_prompt(training_demonstrations, evidence)
    run = runner or _default_runner
    playbook = _complete_recorded_structure(run(agent, prompt), training_demonstrations)
    _validate_against_evidence(
        playbook,
        evidence,
        set(training_demonstrations),
        mutation_recorded=_mutation_recorded(training_demonstrations),
    )
    return playbook
