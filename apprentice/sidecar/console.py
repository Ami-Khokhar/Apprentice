"""Pure context-building for the trust dashboard and run-detail console.

Neither function here touches HTML: each returns a plain ``dict`` of
JSON/Jinja-friendly data so tests can assert on the data directly instead of
scraping rendered markup. ``apprentice.sidecar.app`` is the only place these
contexts are handed to a template or streamed over SSE.

Building the dashboard context is also where idle decay is actually applied
(see ``apprentice.ledger.transitions.apply_idle_decay``): decay is lazy, so a
dashboard render is exactly the kind of read that should resolve it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from apprentice.config import Policy
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.ledger.scoring import effective_level, evaluate_promotion, unique_evidence_counts
from apprentice.ledger.transitions import apply_idle_decay
from apprentice.models import Bucket, Event, EventType, Severity
from apprentice.twin.invariants import CHECK_NAMES

__all__ = [
    "DASHBOARD_SSE_EVENT",
    "POLICY_LABEL",
    "build_capability_context",
    "build_dashboard_context",
    "build_run_context",
    "dashboard_events",
]

POLICY_LABEL = "Demo Policy"

# The named SSE event the dashboard stream emits. The dashboard template's
# EventSource listener must subscribe to exactly this name (a named event
# never fires `onmessage`); a test asserts both sides stay in sync.
DASHBOARD_SSE_EVENT = "dashboard"


def build_dashboard_context(
    repository: Repository, policy: Policy, *, now: float | None = None
) -> dict[str, Any]:
    """The full dashboard page: one capability card per bucket plus the live run panel."""
    resolved_now = time.time() if now is None else now
    buckets = repository.list_buckets()
    buckets_by_name = {bucket.name: bucket for bucket in buckets}
    runs = repository.list_runs()
    capabilities = [
        _capability_card(repository, bucket, policy, buckets_by_name, now=resolved_now)
        for bucket in buckets
    ]
    run_id = _latest_run_id(repository, runs=runs)
    run_context = _empty_run_context() if run_id is None else build_run_context(repository, run_id)
    return {
        "policy_label": POLICY_LABEL,
        "generated_at": resolved_now,
        "overview": _overview(capabilities, runs),
        "capabilities": capabilities,
        "runnable_capabilities": [
            card for card in capabilities if card["kind"] == "playbook" and card["task"]
        ],
        "recent_runs": _recent_runs(repository, runs),
        **run_context,
    }


def build_capability_context(
    repository: Repository, policy: Policy, bucket_id: int, *, now: float | None = None
) -> dict[str, Any]:
    """One capability's trust, evidence, and associated runs for operator review."""
    resolved_now = time.time() if now is None else now
    buckets = repository.list_buckets()
    bucket = repository.get_bucket_by_id(bucket_id)
    buckets_by_name = {item.name: item for item in buckets}
    card = _capability_card(repository, bucket, policy, buckets_by_name, now=resolved_now)
    runs = [run for run in repository.list_runs() if run.bucket_id == bucket_id]
    return {
        "capability": card,
        "recent_runs": _recent_runs(repository, runs),
        "overview": _overview([card], runs),
        "promotion_reasons": [
            _plain_promotion_reason(item) for item in card["next_promotion"]["unmet"]
        ],
    }


def build_run_context(repository: Repository, run_id: str) -> dict[str, Any]:
    """One run's read-only snapshot: twin actions, invariants, approval/veto, outcome."""
    run = repository.get_run(run_id)
    bucket = repository.get_bucket_by_id(run.bucket_id)
    transitions = repository.list_run_transitions(run_id)
    events = repository.list_events(bucket.id, run_id=run_id)

    try:
        checks = repository.get_rehearsal_checks(run_id)
    except ConflictError:
        checks = {}
    checklist = [{"name": name, "status": checks.get(name)} for name in CHECK_NAMES]

    actions = [
        {
            "ordinal": action.ordinal,
            "tool_name": action.tool_name,
            "arguments": action.arguments,
            "effect": action.effect,
        }
        for action in repository.get_run_actions(run_id)
    ]

    verdict_ctx = None
    try:
        verdict = repository.get_verdict(run_id)
        verdict_ctx = {
            "decision": verdict.decision.value,
            "decided_by": verdict.decided_by.value,
            "created_at": verdict.created_at,
        }
    except ConflictError:
        pass

    veto_ctx = None
    try:
        veto = repository.get_veto(run_id)
        veto_ctx = {
            "status": veto.status.value,
            "deadline": veto.deadline,
            "resolved_at": veto.resolved_at,
        }
    except ConflictError:
        pass

    outcome_event = next(
        (event for event in events if event.type in (EventType.RUN_SUCCESS, EventType.RUN_FAILURE)),
        None,
    )
    outcome_ctx = None
    if outcome_event is not None:
        outcome_ctx = {
            "succeeded": outcome_event.type is EventType.RUN_SUCCESS,
            "failure_cause": outcome_event.detail.get("failure_cause"),
            "trust_level_at_outcome": outcome_event.detail.get("trust_level"),
        }

    return {
        "run_id": run.id,
        "bucket_name": bucket.name,
        "bucket_id": bucket.id,
        "state": run.state.value,
        "state_label": run.state.value.replace("_", " "),
        "inputs": run.inputs,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "transitions": [
            {
                "from_state": transition.from_state.value if transition.from_state else None,
                "to_state": transition.to_state.value,
                "reason": transition.reason,
                "created_at": transition.created_at,
            }
            for transition in transitions
        ],
        "checklist": checklist,
        "actions": actions,
        "verdict": verdict_ctx,
        "veto": veto_ctx,
        "production_replay": {
            "executed": run.state.value in ("succeeded", "failed"),
            "action_plan_hash": run.action_plan_hash,
        },
        "outcome": outcome_ctx,
        "approval_urls": {
            "activate": f"/api/runs/{run.id}/activate",
            "approve": f"/api/runs/{run.id}/approval",
            "reject": f"/api/runs/{run.id}/approval",
            "cancel_veto": f"/api/runs/{run.id}/veto",
        },
    }


async def dashboard_events(
    repository: Repository,
    policy: Policy,
    *,
    interval_seconds: float = 2.0,
    clock: Callable[[], float] = time.time,
) -> AsyncIterator[dict[str, Any]]:
    """An unbounded stream of dashboard contexts for SSE-driven refresh."""
    while True:
        yield build_dashboard_context(repository, policy, now=clock())
        await asyncio.sleep(interval_seconds)


def _capability_card(
    repository: Repository,
    bucket: Bucket,
    policy: Policy,
    buckets_by_name: dict[str, Bucket],
    *,
    now: float,
) -> dict[str, Any]:
    # Deliberate write on a read path: idle decay is lazy by design, and this
    # call is idempotent per idle interval (last_decay_at bookmarks it), so a
    # dashboard render or SSE tick can never drain more than one level.
    bucket = apply_idle_decay(repository, bucket, policy, now=now)
    events = repository.list_events(bucket.id)
    training = repository.count_demonstrations(bucket.id, role="training")
    evaluation = evaluate_promotion(
        bucket, events, policy, training_demonstrations=training, now=now
    )
    risk = policy.risk[bucket.risk_class.value]
    risk_cap = min(risk.max_level, 3 if bucket.risk_class.value == "critical" else 4)
    dependency_levels = [
        buckets_by_name[name].level
        for name in bucket.tool_versions
        if name in buckets_by_name and buckets_by_name[name].kind.value == "tool"
    ]

    task = None
    if bucket.activated_at is not None:
        try:
            playbook = repository.get_reviewed_playbook(bucket.id)
            if isinstance(playbook.content, dict):
                task = playbook.content.get("task")
        except ConflictError:
            task = None

    return {
        "bucket_id": bucket.id,
        "name": bucket.name,
        "kind": bucket.kind.value,
        "task": task,
        "risk_class": bucket.risk_class.value,
        "level": bucket.level,
        "effective_level": effective_level(bucket.level, dependency_levels),
        "risk_cap": risk_cap,
        "ladder": [
            {"level": level, "reached": level <= bucket.level, "capped": level > risk_cap}
            for level in range(5)
        ],
        "score": round(evaluation.score, 4),
        "unique_evidence": unique_evidence_counts(events),
        "next_promotion": {
            "eligible": evaluation.eligible,
            "target_level": evaluation.target_level,
            "unmet": list(evaluation.unmet),
            "at_ceiling": evaluation.target_level is None,
        },
        "promotion_reasons": [_plain_promotion_reason(item) for item in evaluation.unmet],
        "path": f"/console/capabilities/{bucket.id}",
        "latest_demotion_cause": _latest_demotion_cause(events, bucket, policy),
    }


def _latest_demotion_cause(
    events: Sequence[Event], bucket: Bucket, policy: Policy
) -> str | None:
    """The genuinely most recent demotion cause: severity failure vs idle decay by timestamp."""
    demotions = [event for event in events if event.severity in (Severity.MAJOR, Severity.CRITICAL)]
    latest = max(demotions, key=lambda event: event.created_at) if demotions else None
    if bucket.last_decay_at is not None and (
        latest is None or bucket.last_decay_at > latest.created_at
    ):
        return f"idle decay (no activity for {policy.decay_idle_days:g}+ days)"
    if latest is not None:
        cause = latest.detail.get("failure_cause", latest.type.value)
        return f"{latest.severity.value} {cause} ({latest.type.value})"
    return None


def _overview(capabilities: Sequence[dict[str, Any]], runs: Sequence[Any]) -> dict[str, Any]:
    highest_level = max((card["effective_level"] for card in capabilities), default=0)
    failed_states = {"failed", "rehearsal_failed", "denied", "vetoed"}
    return {
        "total_capabilities": len(capabilities),
        "activated_capabilities": sum(1 for card in capabilities if card["level"] >= 1),
        "highest_effective_level": highest_level,
        "pending_approvals": sum(1 for run in runs if run.state.value == "approval_pending"),
        "pending_vetoes": sum(1 for run in runs if run.state.value == "veto_pending"),
        "successful_runs": sum(1 for run in runs if run.state.value == "succeeded"),
        "failed_runs": sum(1 for run in runs if run.state.value in failed_states),
    }


def _recent_runs(
    repository: Repository,
    runs: Sequence[Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    recent = sorted(runs, key=lambda run: (run.created_at, run.id), reverse=True)[:limit]
    items: list[dict[str, Any]] = []
    for run in recent:
        bucket = repository.get_bucket_by_id(run.bucket_id)
        items.append(
            {
                "id": run.id,
                "bucket_id": bucket.id,
                "bucket_name": bucket.name,
                "state": run.state.value,
                "state_label": run.state.value.replace("_", " "),
                "created_at": run.created_at,
                "inputs": run.inputs,
                "path": f"/console/runs/{run.id}",
                "approval_urls": {
                    "activate": f"/api/runs/{run.id}/activate",
                    "approve": f"/api/runs/{run.id}/approval",
                    "reject": f"/api/runs/{run.id}/approval",
                    "cancel_veto": f"/api/runs/{run.id}/veto",
                },
            }
        )
    return items


def _plain_promotion_reason(requirement: str) -> str:
    """Turn scoring shorthand into the operator-facing next action."""
    matched = re.fullmatch(r"training demonstrations (\d+)/(\d+)", requirement)
    if matched:
        current, needed = map(int, matched.groups())
        return (
            f"Record {needed - current} more reviewed training demonstration(s) "
            f"({current} of {needed} complete)."
        )
    matched = re.fullmatch(r"unique shadow passes (\d+)/(\d+)", requirement)
    if matched:
        current, needed = map(int, matched.groups())
        return (
            f"Pass {needed - current} more held-out shadow evaluation(s) "
            f"({current} of {needed} complete)."
        )
    matched = re.fullmatch(r"approved successes (\d+)/(\d+)", requirement)
    if matched:
        current, needed = map(int, matched.groups())
        return (
            f"Complete {needed - current} more approved production run(s) "
            f"({current} of {needed} complete)."
        )
    matched = re.fullmatch(r"L3 successes (\d+)/(\d+)", requirement)
    if matched:
        current, needed = map(int, matched.groups())
        return (
            f"Complete {needed - current} more successful L3 run(s) "
            f"({current} of {needed} complete)."
        )
    if requirement == "risk cap reached":
        return "This capability has reached the maximum trust level allowed for its risk class."
    return requirement


def _latest_run_id(repository: Repository, *, runs: Sequence[Any] | None = None) -> str | None:
    runs = repository.list_runs() if runs is None else list(runs)
    if not runs:
        return None
    return max(runs, key=lambda run: (run.created_at, run.id)).id


def _empty_run_context() -> dict[str, Any]:
    return {
        "run_id": None,
        "bucket_name": None,
        "bucket_id": None,
        "state": None,
        "state_label": None,
        "inputs": {},
        "created_at": None,
        "updated_at": None,
        "transitions": [],
        "checklist": [{"name": name, "status": None} for name in CHECK_NAMES],
        "actions": [],
        "verdict": None,
        "veto": None,
        "production_replay": None,
        "outcome": None,
        "approval_urls": None,
    }
