"""The six named rehearsal checks. A rehearsal passes only when all pass.

    1. action_plan_matches_playbook
    2. commit_payload_matches_inputs
    3. no_undeclared_hosts
    4. success_criteria_met
    5. no_improvised_response_on_critical_path
    6. no_network_egress_for_mutations
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apprentice.canonical import digest
from apprentice.induction.induce import Playbook
from apprentice.models import RecordedAction
from apprentice.twin.builder import Twin
from apprentice.twin.responses import ResponseProvenance, RoutedResponse

__all__ = ["CHECK_NAMES", "RehearsalReport", "evaluate_rehearsal"]

CHECK_NAMES: tuple[str, ...] = (
    "action_plan_matches_playbook",
    "commit_payload_matches_inputs",
    "no_undeclared_hosts",
    "success_criteria_met",
    "no_improvised_response_on_critical_path",
    "no_network_egress_for_mutations",
)


@dataclass(frozen=True)
class RehearsalReport:
    passed: bool
    checks: dict[str, bool]


def _action_plan_matches_playbook(
    playbook: Playbook, actions: Sequence[RecordedAction]
) -> bool:
    steps = playbook.steps
    if len(actions) != len(steps):
        return False
    return all(
        action.tool_name == step.action
        and action.effect == step.effect
        and action.arguments.get("anchor") == step.anchor.model_dump(mode="json")
        for action, step in zip(actions, steps, strict=True)
    )


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _commit_payload_matches_inputs(
    inputs: Mapping[str, Any], captured_bodies: Sequence[Mapping[str, Any]]
) -> bool:
    if not captured_bodies:
        return False
    body = captured_bodies[-1]
    fields: Mapping[str, Any] = body.get("fields", {})
    files_by_field = {entry["field"]: entry for entry in body.get("files", [])}

    # Two-directional: the payload may not carry a field or file that no
    # declared input accounts for. Exact name matching, never substrings.
    if not set(fields) <= set(inputs) or not set(files_by_field) <= set(inputs):
        return False

    for name, value in inputs.items():
        if isinstance(value, Mapping) and "sha256" in value:
            file_entry = files_by_field.get(name)
            if file_entry is None:
                return False
            if (
                file_entry.get("filename") != value.get("filename")
                or file_entry.get("sha256") != value.get("sha256")
            ):
                return False
            continue
        if name not in fields:
            if _is_blank(value):
                continue
            return False
        if fields[name] != value:
            return False
    return True


def _no_undeclared_hosts(twin: Twin, visited_hosts: frozenset[str]) -> bool:
    return visited_hosts <= twin.allowed_hosts


def _success_criteria_met(playbook: Playbook, final_response: RoutedResponse | None) -> bool:
    if final_response is None:
        return False
    criteria = playbook.success_criteria
    return (
        final_response.status == criteria.status_code
        and criteria.page_contains in str(final_response.body or "")
    )


def _no_improvised_response_on_critical_path(twin: Twin) -> bool:
    return all(
        mutation.response.provenance is not ResponseProvenance.IMPROVISED
        for mutation in twin.router.captured_mutations
    )


def _no_network_egress_for_mutations(actions: Sequence[RecordedAction], twin: Twin) -> bool:
    """Every commit action corresponds to exactly one captured (never forwarded) mutation."""

    commit_actions = [action for action in actions if action.tool_name == "commit"]
    captured = twin.router.captured_mutations
    if len(commit_actions) != len(captured):
        return False
    return all(
        action.arguments.get("payload_digest") == digest(mutation.body)
        for action, mutation in zip(commit_actions, captured, strict=True)
    )


def evaluate_rehearsal(
    *,
    playbook: Playbook,
    inputs: Mapping[str, Any],
    twin: Twin,
    actions: Sequence[RecordedAction],
) -> RehearsalReport:
    """Run the six named rehearsal checks; a rehearsal passes only when all pass."""

    captured_mutations = twin.router.captured_mutations
    captured_bodies = [mutation.body for mutation in captured_mutations]
    final_response = captured_mutations[-1].response if captured_mutations else None

    checks = {
        "action_plan_matches_playbook": _action_plan_matches_playbook(playbook, actions),
        "commit_payload_matches_inputs": _commit_payload_matches_inputs(inputs, captured_bodies),
        "no_undeclared_hosts": _no_undeclared_hosts(twin, frozenset(twin.router.visited_hosts)),
        "success_criteria_met": _success_criteria_met(playbook, final_response),
        "no_improvised_response_on_critical_path": _no_improvised_response_on_critical_path(twin),
        "no_network_egress_for_mutations": _no_network_egress_for_mutations(actions, twin),
    }
    assert set(checks) == set(CHECK_NAMES)
    return RehearsalReport(passed=all(checks.values()), checks=checks)
