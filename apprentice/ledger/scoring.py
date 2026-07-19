from __future__ import annotations

import math
import time
from collections.abc import Sequence

from apprentice.config import Policy
from apprentice.models import (
    Bucket,
    Event,
    EventType,
    PromotionEvaluation,
    Severity,
)

POSITIVE_EVENTS = frozenset(
    {
        EventType.SHADOW_PASS,
        EventType.TWIN_PASS,
        EventType.APPROVED,
        EventType.APPROVED_WITH_EDITS,
        EventType.RUN_SUCCESS,
        EventType.AUDIT_PASS,
    }
)
NEGATIVE_EVENTS = frozenset(
    {
        EventType.SHADOW_FAIL,
        EventType.TWIN_FAIL,
        EventType.VETOED,
        EventType.RUN_FAILURE,
        EventType.AUDIT_FAIL,
    }
)


def unique_events(events: Sequence[Event]) -> tuple[Event, ...]:
    """Deduplicate by ``evidence_key``, keeping the first occurrence of each key.

    ``events`` rows are unique by evidence_key at write time, but any caller
    assembling its own list (tests, merged queries) must not let an
    accidental duplicate double-count as two pieces of evidence: five
    duplicates of one evidence key must still count as one.
    """
    seen: dict[str, Event] = {}
    for event in events:
        seen.setdefault(event.evidence_key, event)
    return tuple(seen.values())


def unique_evidence_counts(events: Sequence[Event]) -> dict[str, int]:
    """Unique evidence counts per event type, for a capability's evidence summary."""
    counts: dict[str, int] = {}
    for event in unique_events(events):
        counts[event.type.value] = counts.get(event.type.value, 0) + 1
    return counts


def effective_level(bucket_level: int, dependency_levels: Sequence[int] = ()) -> int:
    """A capability can never execute above its weakest declared dependency."""
    return min(bucket_level, *dependency_levels) if dependency_levels else bucket_level


def trust_score(
    events: Sequence[Event],
    *,
    half_life_days: float,
    now: float | None = None,
) -> float:
    events = unique_events(events)
    if not events:
        return 0.0
    timestamp = time.time() if now is None else now
    successes = 0.0
    failures = 0.0
    half_life_seconds = half_life_days * 86_400
    for event in events:
        age = max(0.0, timestamp - event.created_at)
        recency = 0.5 ** (age / half_life_seconds)
        contribution = event.weight * recency
        if event.type in POSITIVE_EVENTS:
            successes += contribution
        elif event.type in NEGATIVE_EVENTS:
            if event.severity is Severity.MINOR:
                contribution *= 3
            elif event.severity in {Severity.MAJOR, Severity.CRITICAL}:
                contribution *= 8
            failures += contribution
    total = successes + failures
    return successes / total if total else 0.0


def evaluate_promotion(
    bucket: Bucket,
    events: Sequence[Event],
    policy: Policy,
    *,
    training_demonstrations: int = 0,
    now: float | None = None,
) -> PromotionEvaluation:
    risk = policy.risk[bucket.risk_class.value]
    risk_cap = min(risk.max_level, 3 if bucket.risk_class.value == "critical" else 4)
    target_level = bucket.level + 1
    current_events = tuple(
        event for event in events if event.playbook_version == bucket.playbook_version
    )
    score = trust_score(current_events, half_life_days=policy.half_life_days, now=now)
    if bucket.level >= risk_cap or target_level > risk_cap or target_level not in policy.levels:
        return PromotionEvaluation(
            eligible=False,
            target_level=None,
            score=score,
            unmet=("risk cap reached",),
        )

    requirements = policy.levels[target_level]
    unmet: list[str] = []
    unique = unique_events(current_events)

    def scaled(required: int | None) -> int | None:
        return None if required is None else math.ceil(required * risk.sample_mult)

    required_training = scaled(requirements.min_training_demonstrations)
    if required_training is not None and training_demonstrations < required_training:
        unmet.append(f"training demonstrations {training_demonstrations}/{required_training}")

    shadow_passes = sum(event.type is EventType.SHADOW_PASS for event in unique)
    required_shadow = scaled(requirements.min_unique_shadow_passes)
    if required_shadow is not None and shadow_passes < required_shadow:
        unmet.append(f"unique shadow passes {shadow_passes}/{required_shadow}")

    approved_successes = sum(event.type is EventType.RUN_SUCCESS for event in unique)
    required_approved = scaled(requirements.min_approved_successes)
    if required_approved is not None and approved_successes < required_approved:
        unmet.append(f"approved successes {approved_successes}/{required_approved}")

    l3_successes = sum(
        event.type is EventType.RUN_SUCCESS and event.detail.get("trust_level") == 3
        for event in unique
    )
    required_l3 = scaled(requirements.min_l3_successes)
    if required_l3 is not None and l3_successes < required_l3:
        unmet.append(f"L3 successes {l3_successes}/{required_l3}")

    if requirements.max_vetoes_recent is not None:
        recent = sorted(unique, key=lambda event: event.created_at)[-10:]
        vetoes = sum(event.type is EventType.VETOED for event in recent)
        if vetoes > requirements.max_vetoes_recent:
            unmet.append(f"recent vetoes {vetoes}/{requirements.max_vetoes_recent} max")

    if requirements.min_score is not None:
        required_score = min(1.0, requirements.min_score + risk.score_bonus)
        if score < required_score:
            unmet.append(f"score {score:.3f}/{required_score:.3f}")

    return PromotionEvaluation(
        eligible=not unmet,
        target_level=target_level,
        score=score,
        unmet=tuple(unmet),
    )


def promotion_eligible(
    bucket: Bucket,
    events: Sequence[Event],
    policy: Policy,
    *,
    training_demonstrations: int = 0,
    now: float | None = None,
) -> bool:
    return evaluate_promotion(
        bucket,
        events,
        policy,
        training_demonstrations=training_demonstrations,
        now=now,
    ).eligible
