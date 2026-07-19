"""Bounded, deterministic Incident Command actors.

Actors are role projections over canonical world state, not autonomous agents.
They can see only their assigned evidence and may submit only named delegation
requests.  The runtime remains the sole authority for every state transition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass


class InvalidActorRequestError(ValueError):
    """Raised for an unknown role or a request outside its bounded policy."""


@dataclass(frozen=True)
class ActorDefinition:
    id: str
    title: str
    goal: str
    knowledge_boundary: str
    visible_artifact_ids: frozenset[str]
    allowed_requests: frozenset[str]


ACTORS: dict[str, ActorDefinition] = {
    "sre-oncall": ActorDefinition(
        id="sre-oncall",
        title="SRE on-call",
        goal="Restore checkout safely and establish the technical cause.",
        knowledge_boundary="Deployment history, raw checkout logs, metrics, and recovery evidence.",
        visible_artifact_ids=frozenset(
            {
                "fixture-failure-deployment",
                "fixture-failure-rejection-log",
                "fixture-failure-metrics",
                "finding-worker-concurrency",
                "fixture-recovery-validation",
            }
        ),
        allowed_requests=frozenset({"investigate_checkout", "recommend_rollback"}),
    ),
    "support-lead": ActorDefinition(
        id="support-lead",
        title="Support lead",
        goal="Represent affected customers and prepare support operations.",
        knowledge_boundary=(
            "Customer impact, queue health, and support-contact reports; "
            "no raw infrastructure logs."
        ),
        visible_artifact_ids=frozenset(
            {"fixture-failure-metrics", "support-checkout-contact-surge"}
        ),
        allowed_requests=frozenset({"prepare_support_brief"}),
    ),
    "customer-comms": ActorDefinition(
        id="customer-comms",
        title="Customer communications",
        goal="Keep customer-facing updates accurate, timely, and approved.",
        knowledge_boundary=(
            "Customer impact, queue health, and approved support context; "
            "no deployment or raw logs."
        ),
        visible_artifact_ids=frozenset(
            {"fixture-failure-metrics", "support-checkout-contact-surge"}
        ),
        allowed_requests=frozenset({"draft_customer_update"}),
    ),
}


# Each request has an explicit, reviewable policy.  None is permitted to alter
# technical metrics.  New consequential delegation must be added here and then
# implemented by an explicit runtime transition rule.
DELEGATION_POLICIES: dict[tuple[str, str], dict[str, str]] = {
    ("sre-oncall", "investigate_checkout"): {
        "title": "SRE investigation requested",
        "detail": "SRE on-call will review the permitted deployment, logs, and metrics.",
        "rule": "actor.delegate.sre-oncall.investigate_checkout",
        "metric_effect": "none",
    },
    ("sre-oncall", "recommend_rollback"): {
        "title": "SRE rollback recommendation requested",
        "detail": "SRE on-call will assess whether the observed evidence supports rollback.",
        "rule": "actor.delegate.sre-oncall.recommend_rollback",
        "metric_effect": "none",
    },
    ("support-lead", "prepare_support_brief"): {
        "title": "Support brief requested",
        "detail": "Support lead will prepare the customer-impact brief from permitted evidence.",
        "rule": "actor.delegate.support-lead.prepare_support_brief",
        "metric_effect": "none",
    },
    ("customer-comms", "draft_customer_update"): {
        "title": "Customer update draft requested",
        "detail": "Customer communications will draft an update from approved incident facts.",
        "rule": "actor.delegate.customer-comms.draft_customer_update",
        "metric_effect": "none",
    },
}


def actor_view(
    actor_id: str,
    *,
    sim_time: int,
    declared: bool,
    investigated: bool,
    rollback: bool,
    communicated: bool,
    metrics: Mapping[str, float],
    artifacts: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Return a role's deterministic knowledge and current recommendations."""
    actor = _actor(actor_id)
    visible = [
        deepcopy(artifact)
        for artifact in artifacts
        if str(artifact["id"]) in actor.visible_artifact_ids
    ]
    return {
        "id": actor.id,
        "title": actor.title,
        "goal": actor.goal,
        "knowledge_boundary": actor.knowledge_boundary,
        "visible_artifacts": visible,
        "recommendations": _recommendations(
            actor_id,
            sim_time=sim_time,
            declared=declared,
            investigated=investigated,
            rollback=rollback,
            communicated=communicated,
            metrics=metrics,
        ),
        "allowed_requests": sorted(actor.allowed_requests),
    }


def delegation_policy(actor_id: str, request: str) -> dict[str, str]:
    """Validate a request and return its immutable, non-metric policy."""
    _actor(actor_id)
    try:
        return deepcopy(DELEGATION_POLICIES[(actor_id, request)])
    except KeyError as error:
        raise InvalidActorRequestError(
            f"Actor request is not permitted: {actor_id}/{request}"
        ) from error


def _actor(actor_id: str) -> ActorDefinition:
    try:
        return ACTORS[actor_id]
    except KeyError as error:
        raise InvalidActorRequestError(f"Unknown incident actor: {actor_id}") from error


def _recommendations(
    actor_id: str,
    *,
    sim_time: int,
    declared: bool,
    investigated: bool,
    rollback: bool,
    communicated: bool,
    metrics: Mapping[str, float],
) -> list[dict[str, str]]:
    """Recommendations derived only from passed canonical state values."""
    if actor_id == "sre-oncall":
        if not investigated:
            return [
                {
                    "request": "investigate_checkout",
                    "reason": "Logs and deployment evidence have not been reviewed.",
                }
            ]
        if not rollback and metrics["error_rate"] >= 5:
            return [
                {
                    "request": "recommend_rollback",
                    "reason": "Confirmed checkout regression remains above the error threshold.",
                }
            ]
        return []
    if actor_id == "support-lead":
        if sim_time >= 3 or metrics["customers_impacted"] >= 100:
            return [
                {
                    "request": "prepare_support_brief",
                    "reason": "Customer impact requires a coordinated support response.",
                }
            ]
        return []
    if not declared:
        return [
            {
                "request": "draft_customer_update",
                "reason": "Await incident declaration before drafting an approved update.",
            }
        ]
    if not communicated:
        return [
            {
                "request": "draft_customer_update",
                "reason": "A declared incident has no published customer update.",
            }
        ]
    return []
