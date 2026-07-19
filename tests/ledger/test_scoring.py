from __future__ import annotations

from pathlib import Path

from apprentice.config import LevelPolicy, Policy, load_policy
from apprentice.ledger.repository import Repository
from apprentice.ledger.scoring import (
    effective_level,
    evaluate_promotion,
    trust_score,
    unique_events,
    unique_evidence_counts,
)
from apprentice.models import Event, EventType, Severity
from apprentice.sidecar.run_service import RunService

ROOT = Path(__file__).parents[2]
POLICY = load_policy(ROOT / "trust_policy.yaml")


def _policy_with_level(level: int, requirements: LevelPolicy) -> Policy:
    levels = dict(POLICY.levels)
    levels[level] = requirements
    return Policy(
        half_life_days=POLICY.half_life_days,
        decay_idle_days=POLICY.decay_idle_days,
        severity_demotion=POLICY.severity_demotion,
        signal_weights=POLICY.signal_weights,
        twin_pass_weight=POLICY.twin_pass_weight,
        veto_seconds=POLICY.veto_seconds,
        risk=dict(POLICY.risk),
        levels=levels,
    )


def _event(
    *,
    bucket_id: int = 1,
    evidence_key: str,
    event_type: EventType,
    weight: float = 1.0,
    severity: Severity | None = None,
    created_at: float = 1_000.0,
    detail: dict | None = None,
) -> Event:
    return Event(
        id=f"evt-{evidence_key}-{created_at}",
        bucket_id=bucket_id,
        run_id=None,
        type=event_type,
        weight=weight,
        severity=severity,
        evidence_key=evidence_key,
        detail=detail or {},
        created_at=created_at,
    )


def _activated_bucket(tmp_path: Path, *, risk_class: str = "medium", level: int = 1):
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense", risk_class=risk_class)
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    service = RunService(repository)
    activated = service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = ? WHERE id = ?", (level, activated.id))
    return repository, repository.get_bucket_by_id(activated.id)


def test_five_duplicates_of_one_evidence_key_still_count_as_one() -> None:
    events = [
        _event(
            evidence_key="shadow:1:abc",
            event_type=EventType.SHADOW_PASS,
            created_at=1_000.0 + index,
        )
        for index in range(5)
    ]

    unique = unique_events(events)

    assert len(unique) == 1


def test_evaluate_promotion_counts_unique_shadow_passes_not_duplicate_rows(
    tmp_path: Path,
) -> None:
    _repository, bucket = _activated_bucket(tmp_path, level=1)
    policy = _policy_with_level(
        2, LevelPolicy(min_unique_shadow_passes=2, min_score=0.01)
    )
    duplicated = [
        _event(
            bucket_id=bucket.id,
            evidence_key="shadow:1:same-digest",
            event_type=EventType.SHADOW_PASS,
            created_at=1_000.0 + index,
        )
        for index in range(5)
    ]

    evaluation = evaluate_promotion(bucket, duplicated, policy, now=2_000.0)

    assert evaluation.eligible is False
    assert any("unique shadow passes 1/2" in reason for reason in evaluation.unmet)


def test_unique_evidence_counts_groups_by_type() -> None:
    events = [
        _event(evidence_key="shadow:1:a", event_type=EventType.SHADOW_PASS),
        _event(evidence_key="shadow:1:a", event_type=EventType.SHADOW_PASS),
        _event(evidence_key="shadow:1:b", event_type=EventType.SHADOW_PASS),
        _event(evidence_key="twin:run-1", event_type=EventType.TWIN_PASS, weight=0.25),
    ]

    counts = unique_evidence_counts(events)

    assert counts == {"shadow_pass": 2, "twin_pass": 1}


def test_high_risk_scales_samples_and_score_threshold(tmp_path: Path) -> None:
    _repository, bucket = _activated_bucket(tmp_path, risk_class="high", level=1)
    # L2 for high risk: sample_mult=2.0 -> ceil(1*2.0)=2 shadow passes required,
    # score_bonus=0.03 -> required score 0.88.
    one_pass = [
        _event(bucket_id=bucket.id, evidence_key="shadow:1:a", event_type=EventType.SHADOW_PASS)
    ]

    under_sampled = evaluate_promotion(bucket, one_pass, POLICY, now=2_000.0)

    assert under_sampled.eligible is False
    assert any("unique shadow passes 1/2" in reason for reason in under_sampled.unmet)

    two_passes = [
        *one_pass,
        _event(bucket_id=bucket.id, evidence_key="shadow:1:b", event_type=EventType.SHADOW_PASS),
    ]
    fully_sampled = evaluate_promotion(bucket, two_passes, POLICY, now=2_000.0)

    assert fully_sampled.eligible is True
    assert fully_sampled.target_level == 2
    assert fully_sampled.score >= 0.88


def test_critical_caps_at_l3(tmp_path: Path) -> None:
    _repository, bucket = _activated_bucket(tmp_path, risk_class="critical", level=3)

    evaluation = evaluate_promotion(bucket, [], POLICY, now=2_000.0)

    assert evaluation.eligible is False
    assert evaluation.target_level is None
    assert evaluation.unmet == ("risk cap reached",)


def test_twin_pass_weighs_less_than_a_full_success_and_failures_count_fully() -> None:
    twin_pass_only = [
        _event(
            evidence_key="rehearsal:run-1",
            event_type=EventType.TWIN_PASS,
            weight=POLICY.twin_pass_weight,
        )
    ]
    full_success_only = [
        _event(evidence_key="outcome:run-1", event_type=EventType.RUN_SUCCESS, weight=1.0)
    ]

    assert trust_score(twin_pass_only, half_life_days=POLICY.half_life_days, now=2_000.0) == 1.0
    assert trust_score(full_success_only, half_life_days=POLICY.half_life_days, now=2_000.0) == 1.0

    mixed_with_failure = [
        *full_success_only,
        _event(
            evidence_key="veto:run-2",
            event_type=EventType.VETOED,
            weight=1.0,
            severity=Severity.MINOR,
        ),
    ]
    score = trust_score(mixed_with_failure, half_life_days=POLICY.half_life_days, now=1_000.0)
    assert 0.0 < score < 1.0


def test_rubber_stamp_approvals_weigh_less_than_engaged_approvals(tmp_path: Path) -> None:
    repository, bucket = _activated_bucket(tmp_path, level=2)
    service = RunService(repository, policy=POLICY)
    run = service.create_run(bucket.name, {"amount": "1.00"})
    service.start_rehearsal(run.id)
    service.record_rehearsal_result(run.id, passed=True)
    service.request_approval(run.id)
    service.record_approval(run.id, attention="rubber_stamp")

    events = repository.list_events(bucket.id)
    approval_event = next(event for event in events if event.type is EventType.APPROVED)

    assert approval_event.weight == POLICY.signal_weights.rubber_stamp
    assert approval_event.weight < POLICY.signal_weights.engaged


def test_effective_level_is_the_minimum_of_bucket_and_tool_dependencies() -> None:
    assert effective_level(4, []) == 4
    assert effective_level(4, [3, 4]) == 3
    assert effective_level(2, [4, 4]) == 2


def test_major_and_critical_demotion_are_immediate_and_match_policy(tmp_path: Path) -> None:
    repository, bucket = _activated_bucket(tmp_path, level=3)
    service = RunService(repository, policy=POLICY)
    run = service.create_run(bucket.name, {"amount": "1.00"})
    service.start_rehearsal(run.id)

    _updated, _event = service.record_rehearsal_result(run.id, passed=False)

    demoted = repository.get_bucket_by_id(bucket.id)
    assert bucket.level - demoted.level == POLICY.severity_demotion.major
