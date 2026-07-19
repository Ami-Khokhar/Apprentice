from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
from threading import Barrier

import pytest

from apprentice.canonical import digest
from apprentice.config import load_policy
from apprentice.ledger.repository import Repository
from apprentice.ledger.transitions import apply_idle_decay, confirm_promotion
from apprentice.models import (
    EventType,
    FailureCause,
    RecordedAction,
    RunState,
    Severity,
    VerdictActor,
    VerdictDecision,
)
from apprentice.sidecar.run_service import (
    ConflictError,
    InvalidTransitionError,
    RunService,
)
from apprentice.twin.invariants import CHECK_NAMES

POLICY = load_policy(Path(__file__).parents[2] / "trust_policy.yaml")
ALL_CHECKS_PASS = dict.fromkeys(CHECK_NAMES, True)


def make_run(tmp_path, *, fault_injector=None):
    repository = Repository(tmp_path / "apprentice.db", fault_injector=fault_injector)
    bucket = repository.create_bucket(name="file-expense")
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    identifiers = count(1)
    service = RunService(repository, id_factory=lambda: f"id-{next(identifiers)}")
    service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    run = service.create_run("file-expense", {"amount": "42.00"})
    return repository, service, run


def test_legal_transition_is_persisted_and_auditable(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)

    transitioned = service.start_rehearsal(run.id)

    assert transitioned.state is RunState.REHEARSING
    audit = repository.list_run_transitions(run.id)
    assert [(item.from_state, item.to_state, item.reason) for item in audit] == [
        (None, RunState.CREATED, "run_created"),
        (RunState.CREATED, RunState.REHEARSING, "rehearsal_started"),
    ]


def test_illegal_transition_does_not_mutate_state_or_audit_log(tmp_path) -> None:
    repository, _service, run = make_run(tmp_path)

    from apprentice.ledger.transitions import validate_transition

    with pytest.raises(InvalidTransitionError):
        validate_transition(RunState.CREATED, RunState.AUTHORIZED)

    assert repository.get_run(run.id).state is RunState.CREATED
    assert len(repository.list_run_transitions(run.id)) == 1


def test_stale_expected_state_is_a_conflict(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)

    with pytest.raises(ConflictError):
        service.start_rehearsal(run.id)

    assert repository.get_run(run.id).state is RunState.REHEARSING
    assert len(repository.list_run_transitions(run.id)) == 2


@pytest.mark.parametrize(
    ("start", "finish"),
    [
        (RunState.REHEARSING, RunState.REHEARSAL_FAILED),
        (RunState.REHEARSING, RunState.REHEARSED),
        (RunState.REHEARSED, RunState.APPROVAL_PENDING),
        (RunState.REHEARSED, RunState.VETO_PENDING),
        (RunState.REHEARSED, RunState.AUTHORIZED),
        (RunState.APPROVAL_PENDING, RunState.AUTHORIZED),
        (RunState.APPROVAL_PENDING, RunState.DENIED),
        (RunState.VETO_PENDING, RunState.AUTHORIZED),
        (RunState.VETO_PENDING, RunState.VETOED),
        (RunState.AUTHORIZED, RunState.EXECUTING),
        (RunState.AUTHORIZED, RunState.EXPIRED),
        (RunState.EXECUTING, RunState.SUCCEEDED),
        (RunState.EXECUTING, RunState.FAILED),
    ],
)
def test_declared_lifecycle_edges_are_legal(start: RunState, finish: RunState) -> None:
    from apprentice.ledger.transitions import validate_transition

    validate_transition(start, finish)


def test_concurrent_compare_and_swap_has_exactly_one_winner(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    barrier = Barrier(2)

    def attempt(target: RunState) -> str:
        barrier.wait()
        try:
            assert target is RunState.REHEARSING
            service.start_rehearsal(run.id)
        except (ConflictError, InvalidTransitionError):
            return "lost"
        return "won"

    # Both calls use the only legal target. The second must observe the committed state.
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [RunState.REHEARSING, RunState.REHEARSING]))

    assert sorted(results) == ["lost", "won"]
    assert len(repository.list_run_transitions(run.id)) == 2


def test_run_service_does_not_expose_generic_transition(tmp_path) -> None:
    _, service, _ = make_run(tmp_path)

    assert not hasattr(service, "transition")


def test_approval_state_verdict_and_evidence_commit_together(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)
    service.record_rehearsal_result(run.id, passed=True, detail={"checks": "all"})
    service.request_approval(run.id)

    authorized, verdict, event = service.record_approval(run.id, detail={"review": "accepted"})
    retry = service.record_approval(run.id, detail={"review": "accepted"})

    assert authorized.state is RunState.AUTHORIZED
    assert verdict.decision is VerdictDecision.APPROVED
    assert event.evidence_key == f"approval:{run.id}"
    assert retry == (authorized, verdict, event)
    assert repository.count_events(run.bucket_id) == 2


def test_failed_named_workflow_leaves_state_event_and_verdict_unchanged(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)

    with pytest.raises(ConflictError):
        service.record_approval(run.id)

    assert repository.get_run(run.id).state is RunState.CREATED
    assert repository.count_events(run.bucket_id) == 0
    with pytest.raises(ConflictError, match="no verdict"):
        repository.get_verdict(run.id)


def test_failed_outcome_retry_uses_original_trust_snapshot_and_demotes_once(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = 3 WHERE id = ?", (run.bucket_id,))
    service.start_rehearsal(run.id)
    service.record_rehearsal_result(run.id, passed=True)
    service.request_approval(run.id)
    service.record_approval(run.id)
    service.start_execution(run.id)

    first = service.record_verified_outcome(
        run.id,
        succeeded=False,
        failure_cause=FailureCause.PLAN_MISMATCH,
        detail={"check": "commit_payload"},
    )
    retry = service.record_verified_outcome(
        run.id,
        succeeded=False,
        failure_cause=FailureCause.PLAN_MISMATCH,
        detail={"check": "commit_payload"},
    )

    assert retry == first
    assert first[1].detail["trust_level"] == 3
    assert repository.get_bucket_by_id(run.bucket_id).level == 2
    assert repository.count_events(run.bucket_id) == 3


def test_compound_write_rolls_back_state_audit_verdict_event_and_demotion(tmp_path) -> None:
    fail = {"enabled": False}

    def inject(point: str) -> None:
        if fail["enabled"] and point == "before_compound_commit":
            raise RuntimeError("injected transaction failure")

    repository, service, run = make_run(tmp_path, fault_injector=inject)
    service.start_rehearsal(run.id)
    service.record_rehearsal_result(run.id, passed=True)
    service.request_approval(run.id)
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = 3 WHERE id = ?", (run.bucket_id,))
    transitions_before = repository.list_run_transitions(run.id)
    events_before = repository.list_events(run.bucket_id)
    fail["enabled"] = True

    with pytest.raises(RuntimeError, match="injected"):
        repository._transition_with_event(
            run_id=run.id,
            expected_state=RunState.APPROVAL_PENDING,
            new_state=RunState.DENIED,
            event_id="rollback-event",
            event_type=EventType.VETOED,
            weight=1.0,
            severity=Severity.MAJOR,
            evidence_key=f"approval:{run.id}",
            detail={"reason": "unsafe plan"},
            demotion_levels=1,
            created_at=123.0,
            reason="human_denied",
            verdict=("rollback-verdict", VerdictDecision.DENIED, VerdictActor.HUMAN),
        )

    assert repository.get_run(run.id).state is RunState.APPROVAL_PENDING
    assert repository.list_run_transitions(run.id) == transitions_before
    assert repository.list_events(run.bucket_id) == events_before
    assert repository.get_bucket_by_id(run.bucket_id).level == 3
    with pytest.raises(ConflictError, match="no verdict"):
        repository.get_verdict(run.id)


def _sample_actions() -> tuple[RecordedAction, ...]:
    navigate = RecordedAction(
        ordinal=0, tool_name="navigate", arguments={"path": "/expense"}, effect="observe",
        arguments_digest=digest({"path": "/expense"}),
    )
    commit = RecordedAction(
        ordinal=1, tool_name="commit", arguments={"payload_digest": "abc"}, effect="commit",
        arguments_digest=digest({"payload_digest": "abc"}),
    )
    return (navigate, commit)


def test_record_rehearsal_result_with_actions_stores_plan_hash_rehearsal_and_run_actions(
    tmp_path,
) -> None:
    repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)
    actions = _sample_actions()

    updated, event = service.record_rehearsal_result(
        run.id,
        passed=True,
        checks=ALL_CHECKS_PASS,
        trace_ref="trace-1",
        actions=actions,
    )

    assert updated.state is RunState.REHEARSED
    expected_hash = digest([action.model_dump(mode="json") for action in actions])
    assert updated.action_plan_hash == expected_hash
    assert event.type is EventType.TWIN_PASS

    with repository.db.transaction() as connection:
        rehearsal_row = connection.execute(
            "SELECT * FROM rehearsals WHERE run_id = ?", (run.id,)
        ).fetchone()
        action_rows = connection.execute(
            "SELECT * FROM run_actions WHERE run_id = ? ORDER BY ordinal", (run.id,)
        ).fetchall()

    assert rehearsal_row["passed"] == 1
    assert rehearsal_row["trace_ref"] == "trace-1"
    assert [row["tool_name"] for row in action_rows] == ["navigate", "commit"]
    assert all(row["status"] == "planned" for row in action_rows)


def test_record_rehearsal_result_with_actions_is_idempotent(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)
    actions = _sample_actions()

    first = service.record_rehearsal_result(
        run.id, passed=True, checks=ALL_CHECKS_PASS, trace_ref="trace-1", actions=actions
    )
    second = service.record_rehearsal_result(
        run.id, passed=True, checks=ALL_CHECKS_PASS, trace_ref="trace-1", actions=actions
    )

    assert first == second
    assert repository.count_events(run.bucket_id) == 1
    with repository.db.transaction() as connection:
        rehearsal_count = connection.execute(
            "SELECT COUNT(*) FROM rehearsals WHERE run_id = ?", (run.id,)
        ).fetchone()[0]
        action_count = connection.execute(
            "SELECT COUNT(*) FROM run_actions WHERE run_id = ?", (run.id,)
        ).fetchone()[0]
    assert rehearsal_count == 1
    assert action_count == 2


def test_record_rehearsal_result_requires_checks_and_trace_ref_when_actions_supplied(
    tmp_path,
) -> None:
    _repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)

    with pytest.raises(ValueError, match="checks and trace_ref"):
        service.record_rehearsal_result(run.id, passed=True, actions=_sample_actions())


def test_record_rehearsal_result_without_actions_keeps_legacy_behavior(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)

    updated, _event = service.record_rehearsal_result(run.id, passed=True, detail={"checks": "all"})

    assert updated.state is RunState.REHEARSED
    assert updated.action_plan_hash is None
    with repository.db.transaction() as connection:
        rehearsal_row = connection.execute(
            "SELECT * FROM rehearsals WHERE run_id = ?", (run.id,)
        ).fetchone()
    assert rehearsal_row is None


def test_failed_rehearsal_keeps_a_null_plan_hash_but_retains_audit_rows(tmp_path) -> None:
    repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)

    updated, event = service.record_rehearsal_result(
        run.id,
        passed=False,
        checks={**ALL_CHECKS_PASS, "commit_payload_matches_inputs": False},
        trace_ref="trace-fail",
        actions=_sample_actions(),
    )

    assert updated.state is RunState.REHEARSAL_FAILED
    assert updated.action_plan_hash is None
    assert event.type is EventType.TWIN_FAIL
    with repository.db.transaction() as connection:
        rehearsal_row = connection.execute(
            "SELECT passed FROM rehearsals WHERE run_id = ?", (run.id,)
        ).fetchone()
        action_count = connection.execute(
            "SELECT COUNT(*) FROM run_actions WHERE run_id = ?", (run.id,)
        ).fetchone()[0]
    assert rehearsal_row["passed"] == 0
    assert action_count == 2


def test_passing_rehearsal_rejects_inconsistent_checks_and_empty_plan(tmp_path) -> None:
    _repository, service, run = make_run(tmp_path)
    service.start_rehearsal(run.id)

    with pytest.raises(ValueError, match="passed flag"):
        service.record_rehearsal_result(
            run.id,
            passed=True,
            checks={**ALL_CHECKS_PASS, "no_undeclared_hosts": False},
            trace_ref="forged",
            actions=_sample_actions(),
        )
    with pytest.raises(ValueError, match="final commit"):
        service.record_rehearsal_result(
            run.id,
            passed=True,
            checks=ALL_CHECKS_PASS,
            trace_ref="empty",
            actions=(),
        )


def _idle_bucket(tmp_path, *, level: int = 3, last_activity_at: float = 0.0):
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE buckets SET level = ?, last_activity_at = ? WHERE id = ?",
            (level, last_activity_at, bucket.id),
        )
    return repository, repository.get_bucket_by_id(bucket.id)


def test_decay_does_not_apply_before_the_idle_interval_elapses(tmp_path) -> None:
    repository, bucket = _idle_bucket(tmp_path, level=3, last_activity_at=0.0)
    interval = POLICY.decay_idle_days * 86_400

    decayed = apply_idle_decay(repository, bucket, POLICY, now=interval - 1)

    assert decayed.level == 3
    assert decayed.last_decay_at is None


def test_decay_applies_lazily_after_the_idle_interval_elapses(tmp_path) -> None:
    repository, bucket = _idle_bucket(tmp_path, level=3, last_activity_at=0.0)
    interval = POLICY.decay_idle_days * 86_400

    decayed = apply_idle_decay(repository, bucket, POLICY, now=interval)

    assert decayed.level == 2
    assert decayed.last_decay_at == interval
    assert repository.get_bucket_by_id(bucket.id).level == 2


def test_decay_does_not_repeatedly_drain_levels_on_every_read(tmp_path) -> None:
    repository, bucket = _idle_bucket(tmp_path, level=3, last_activity_at=0.0)
    interval = POLICY.decay_idle_days * 86_400

    first_read = apply_idle_decay(repository, bucket, POLICY, now=interval)
    second_read = apply_idle_decay(repository, first_read, POLICY, now=interval + 1)
    third_read = apply_idle_decay(repository, second_read, POLICY, now=interval + 100)

    assert first_read.level == 2
    assert second_read.level == 2
    assert third_read.level == 2
    assert repository.get_bucket_by_id(bucket.id).level == 2


def test_decay_applies_again_after_a_second_full_idle_interval(tmp_path) -> None:
    repository, bucket = _idle_bucket(tmp_path, level=3, last_activity_at=0.0)
    interval = POLICY.decay_idle_days * 86_400

    first_decay = apply_idle_decay(repository, bucket, POLICY, now=interval)
    second_decay = apply_idle_decay(repository, first_decay, POLICY, now=interval * 2)

    assert first_decay.level == 2
    assert second_decay.level == 1
    assert repository.get_bucket_by_id(bucket.id).level == 1


def test_decay_floors_at_level_zero(tmp_path) -> None:
    repository, bucket = _idle_bucket(tmp_path, level=0, last_activity_at=0.0)
    interval = POLICY.decay_idle_days * 86_400

    decayed = apply_idle_decay(repository, bucket, POLICY, now=interval)

    assert decayed.level == 0
    assert repository.get_bucket_by_id(bucket.id).level == 0


def _promotable_bucket(tmp_path):
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense", risk_class="low")
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    repository.add_demonstration(bucket.id, "heldout-1", "heldout-1", "heldout")
    service = RunService(repository, policy=POLICY)
    activated = service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    return repository, service, activated


def test_confirm_promotion_applies_only_when_still_eligible(tmp_path) -> None:
    repository, service, bucket = _promotable_bucket(tmp_path)
    service.record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=bucket.playbook_version,
        heldout_artifact_digest="heldout-1",
        passed=True,
        detail={},
    )

    promoted = confirm_promotion(repository, bucket.id, POLICY, now=1_000.0)

    assert promoted.level == 2
    assert repository.get_bucket_by_id(bucket.id).level == 2


def test_confirm_promotion_raises_when_no_longer_eligible(tmp_path) -> None:
    repository, _service, bucket = _promotable_bucket(tmp_path)

    with pytest.raises(ConflictError):
        confirm_promotion(repository, bucket.id, POLICY, now=1_000.0)

    assert repository.get_bucket_by_id(bucket.id).level == 1
