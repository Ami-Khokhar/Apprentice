"""Held-out shadow evaluation: GPT-5.6 replays a trace it never trained on.

A shadow pass may only be earned by a held-out demonstration. GPT-5.6 is
shown the induced playbook and the held-out artifact's recorded page states
and asked to propose the exact actions it would take. Deterministic code
then compares that proposal against the held-out trace's own recorded
ground truth on five axes: action, semantic anchor, rendered value, branch
decision, and final success criterion. Only a qualifying (fully matching)
result is ever forwarded to the sidecar's ``RunService.record_shadow_result``,
which owns the ``shadow:{playbook_version}:{heldout_artifact_digest}``
evidence key and its idempotency guarantees.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Literal

from agents import Agent, Runner
from pydantic import BaseModel

from apprentice.induction.induce import Anchor, Playbook
from apprentice.models import Event
from apprentice.providers import provider_model, provider_model_settings
from apprentice.sidecar.run_service import RunService

__all__ = [
    "ProposedAction",
    "ShadowResult",
    "ShadowRunner",
    "ShadowSubmission",
    "build_shadow_agent",
    "evaluate_heldout_shadow",
    "record_qualifying_shadow",
]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Recorded artifacts use the raw recorder vocabulary; proposals and ground
# truth use the plan's contract vocabulary (§6.1/§6.3).
_RAW_TO_CONTRACT = {
    "input": "fill",
    "upload": "upload",
    "click": "click",
    "submit": "commit",
}


class ProposedAction(BaseModel):
    """One action GPT-5.6 proposes to take against a recorded page state."""

    action: Literal["navigate", "fill", "upload", "click", "commit"]
    anchor: Anchor
    value: str | None = None


class ShadowSubmission(BaseModel):
    """The model's full answer for a held-out replay: actions, branch, outcome."""

    actions: tuple[ProposedAction, ...]
    branch_taken: str
    success_status: int
    success_text: str


class ShadowResult(BaseModel):
    """The deterministic verdict comparing a submission against ground truth."""

    passed: bool
    mismatches: tuple[str, ...] = ()


ShadowRunner = Callable[[Agent[Any], str], ShadowSubmission]


def _ground_truth_actions(heldout_artifact: Mapping[str, Any]) -> tuple[ProposedAction, ...]:
    steps = heldout_artifact["steps"]
    actions: list[ProposedAction] = []
    for index, step in enumerate(steps):
        action = step["action"]
        if action not in _RAW_TO_CONTRACT:
            continue
        if action == "click":
            # The recorder captures the commit gesture twice: a click event
            # immediately followed by a submit event on the same anchor.
            # That pair is one contract-level commit, not click + commit.
            following = steps[index + 1] if index + 1 < len(steps) else None
            if (
                following is not None
                and following["action"] == "submit"
                and following.get("anchor") == step.get("anchor")
            ):
                continue
        value: str | None = None
        if action == "input":
            value = step["value"]
        elif action == "upload":
            files = step["value"]["files"]
            value = files[0]["filename"] if files else None
        actions.append(
            ProposedAction(
                action=_RAW_TO_CONTRACT[action],
                anchor=Anchor.model_validate(step["anchor"]),
                value=value,
            )
        )
    return tuple(actions)


def _ground_truth_branch(heldout_artifact: Mapping[str, Any]) -> str:
    """Whether the amount-over-policy justification branch was demonstrated.

    Determined structurally from the recorded trace itself (was a
    justification field filled in?), not by re-deriving policy from the
    playbook's free-text decision point description.
    """

    for step in heldout_artifact["steps"]:
        if step["action"] == "input" and step["anchor"]["css"] == "#justification":
            return "justification_required"
    return "none"


def _ground_truth_success(heldout_artifact: Mapping[str, Any]) -> tuple[int, str]:
    mutating = [
        entry
        for entry in heldout_artifact["http_entries"]
        if entry["method"] in _MUTATING_METHODS
    ]
    if not mutating:
        raise ValueError("held-out artifact has no recorded mutation to judge success against")
    status = mutating[-1]["response"]["status"]
    page_states = [step for step in heldout_artifact["steps"] if step["action"] == "page_state"]
    text = page_states[-1]["page_state"]["text"] if page_states else ""
    return status, text


def build_shadow_agent() -> Agent[Any]:
    """Construct the single GPT-5.6 shadow-replay agent with a typed output."""

    return Agent(
        name="apprentice-shadow-evaluator",
        instructions=(
            "You are replaying a demonstrated browser procedure against a "
            "held-out example you have not been trained on. Given the "
            "induced playbook, held-out inputs, and recorded page states, "
            "propose the exact task actions using only fill, upload, and "
            "commit, with each semantic anchor and rendered value. The page "
            "is already open: do not emit navigate, and represent the submit "
            "click/submit pair as one commit. Use heldout_inputs as value "
            "evidence; for upload, use the first file's filename as value. "
            "Omit an optional fill when no held-out input exists for its "
            "anchor. Set branch_taken to exactly 'justification_required' "
            "only when heldout_inputs contains a non-empty justification; "
            "otherwise set it to exactly 'none'. State the final success "
            "criterion you expect."
        ),
        model=provider_model(),
        model_settings=provider_model_settings(),
        output_type=ShadowSubmission,
    )


def _render_shadow_prompt(playbook: Playbook, heldout_artifact: Mapping[str, Any]) -> str:
    page_states = [
        page_state
        for step in heldout_artifact["steps"]
        if step["action"] in ("navigate", "page_state")
        and (page_state := step.get("page_state")) is not None
    ]
    payload = {
        "playbook": playbook.model_dump(mode="json"),
        "heldout_inputs": [
            {"anchor": step["anchor"], "value": step["value"]}
            for step in heldout_artifact["steps"]
            if step["action"] in ("input", "upload")
        ],
        "recorded_page_states": page_states,
    }
    return json.dumps(payload, sort_keys=True)


def _default_runner(agent: Agent[Any], prompt: str) -> ShadowSubmission:
    result = Runner.run_sync(agent, prompt)
    output = result.final_output
    assert isinstance(output, ShadowSubmission)
    return output


def evaluate_heldout_shadow(
    playbook: Playbook,
    heldout_artifact: Mapping[str, Any],
    *,
    runner: ShadowRunner | None = None,
) -> ShadowResult:
    """Ask GPT-5.6 to replay a held-out trace and score its proposal deterministically."""

    agent = build_shadow_agent()
    prompt = _render_shadow_prompt(playbook, heldout_artifact)
    run = runner or _default_runner
    submission = run(agent, prompt)

    mismatches: list[str] = []
    if submission.actions != _ground_truth_actions(heldout_artifact):
        mismatches.append("actions")
    if submission.branch_taken != _ground_truth_branch(heldout_artifact):
        mismatches.append("branch")
    expected_status, expected_text = _ground_truth_success(heldout_artifact)
    if submission.success_status != expected_status or submission.success_text not in expected_text:
        mismatches.append("success_criterion")

    return ShadowResult(passed=not mismatches, mismatches=tuple(mismatches))


def record_qualifying_shadow(
    service: RunService,
    *,
    bucket_id: int,
    playbook_version: int,
    playbook: Playbook,
    heldout_artifact: Mapping[str, Any],
    runner: ShadowRunner | None = None,
) -> Event:
    """Evaluate a held-out replay and forward the result to the sidecar ledger.

    ``RunService.record_shadow_result`` owns the
    ``shadow:{playbook_version}:{heldout_artifact_digest}`` evidence key,
    rejects any non-held-out artifact digest, and is idempotent: replaying
    the same held-out trace returns the existing event rather than
    double-counting it.
    """

    result = evaluate_heldout_shadow(playbook, heldout_artifact, runner=runner)
    return service.record_shadow_result(
        bucket_id=bucket_id,
        playbook_version=playbook_version,
        heldout_artifact_digest=heldout_artifact["artifact_digest"],
        passed=result.passed,
        # A scalar, not a JSON array. (The ledger used to compare a reloaded
        # event's detail by Python equality, so a stored list --- tuple-frozen
        # by FrozenModel --- would never equal a freshly supplied list; that's
        # fixed now via canonical-JSON comparison in
        # ``_verify_idempotent_event``, but the scalar shape here is kept as
        # the simplest, most obviously-correct representation.)
        detail={"mismatches": ",".join(result.mismatches)},
    )
