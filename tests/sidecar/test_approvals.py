from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from agents import Agent, function_tool
from fastapi.testclient import TestClient

from apprentice.canonical import digest
from apprentice.config import load_policy
from apprentice.executor.sdk_state import SDKStateStore, build_pending_interruption_state
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.models import RecordedAction, RunState, VetoStatus
from apprentice.sidecar.app import build_sidecar
from apprentice.sidecar.approval_service import (
    ApprovalOutcome,
    ApprovalService,
    ChecksFailedError,
    NotRehearsedError,
    VetoAlreadyResolvedError,
)
from apprentice.sidecar.run_service import RunService
from apprentice.twin.invariants import CHECK_NAMES

_ALL_CHECKS_PASS = dict.fromkeys(CHECK_NAMES, True)
POLICY = load_policy(Path(__file__).parents[2] / "trust_policy.yaml")


@function_tool(needs_approval=True)
async def _activate_rehearsed_plan(run_id: str) -> str:
    return f"activated {run_id}"


def _production_agent() -> Agent:
    return Agent(
        name="apprentice-production-executor",
        instructions="Call activate_rehearsed_plan with the given run_id.",
        tools=[_activate_rehearsed_plan],
    )


def _actions() -> list[RecordedAction]:
    navigate = RecordedAction(
        ordinal=0,
        tool_name="navigate",
        arguments={"path": "/expense"},
        arguments_digest=digest({"path": "/expense"}),
        effect="observe",
    )
    commit_arguments = {
        "anchor": {"css": "button", "role": "button", "name": "Submit expense"},
        "payload_digest": "deadbeef",
        "method": "POST",
        "url": "http://127.0.0.1/expense",
    }
    commit = RecordedAction(
        ordinal=1,
        tool_name="commit",
        arguments=commit_arguments,
        arguments_digest=digest(commit_arguments),
        effect="commit",
    )
    return [navigate, commit]


def _set_bucket_level(repository: Repository, bucket_id: int, level: int) -> None:
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = ? WHERE id = ?", (level, bucket_id))


def _rehearsed_run(
    repository: Repository,
    run_service: RunService,
    *,
    level: int,
    actions: list[RecordedAction] | None = None,
    checks: dict[str, bool] | None = None,
):
    bucket = repository.create_bucket(name=f"file-expense-l{level}")
    repository.add_demonstration(bucket.id, "training-1", f"training-1-l{level}", "training")
    repository.add_demonstration(bucket.id, "training-2", f"training-2-l{level}", "training")
    run_service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    _set_bucket_level(repository, bucket.id, level)
    run = run_service.create_run(bucket.name, {"amount": "42.00"})
    run_service.start_rehearsal(run.id)
    updated_run, _event = run_service.record_rehearsal_result(
        run.id,
        passed=True,
        checks=checks if checks is not None else _ALL_CHECKS_PASS,
        trace_ref="twin-run-1",
        actions=actions if actions is not None else _actions(),
    )
    return updated_run, bucket


def _service(repository: Repository, *, now: float = 1_000.0, veto_seconds: float = 5.0):
    run_service = RunService(repository)
    approval_service = ApprovalService(
        run_service, clock=lambda: now, veto_seconds=veto_seconds, id_factory=_counting_ids()
    )
    return run_service, approval_service


def _counting_ids():
    counter = iter(range(10_000))

    def make_id() -> str:
        return f"id-{next(counter)}"

    return make_id


def test_caller_cannot_activate_a_created_run(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    bucket = repository.create_bucket(name="file-expense")
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    run_service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    run = run_service.create_run(bucket.name, {"amount": "42.00"})

    try:
        approval_service.begin_activation(run.id)
        raised = False
    except NotRehearsedError:
        raised = True
    assert raised
    assert repository.get_run(run.id).state is RunState.CREATED


def test_caller_cannot_activate_a_rehearsal_failed_run(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    bucket = repository.create_bucket(name="file-expense")
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    run_service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    run = run_service.create_run(bucket.name, {"amount": "42.00"})
    run_service.start_rehearsal(run.id)
    run_service.record_rehearsal_result(run.id, passed=False)

    try:
        approval_service.begin_activation(run.id)
        raised = False
    except NotRehearsedError:
        raised = True
    assert raised


def test_stale_stored_checks_reject_activation_and_fail_the_run(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    failing_checks = {**_ALL_CHECKS_PASS, "no_undeclared_hosts": False}
    run, _bucket = _rehearsed_run(repository, run_service, level=4)
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE rehearsals SET checks_json = ? WHERE run_id = ?",
            (json.dumps(failing_checks, sort_keys=True), run.id),
        )

    try:
        approval_service.begin_activation(run.id)
        raised = False
    except ChecksFailedError:
        raised = True
    assert raised
    assert repository.get_run(run.id).state is RunState.REHEARSAL_FAILED


def test_tampered_action_plan_hash_rejects_activation(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=4)
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE run_actions SET arguments_digest = 'tampered' WHERE run_id = ? AND ordinal = 0",
            (run.id,),
        )

    try:
        approval_service.begin_activation(run.id)
        raised = False
    except ChecksFailedError:
        raised = True
    assert raised
    assert repository.get_run(run.id).state is RunState.REHEARSAL_FAILED


def test_l1_denies_activation_immediately(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=1)

    outcome = approval_service.begin_activation(run.id)

    assert outcome.kind == "denied"
    assert repository.get_run(run.id).state is RunState.DENIED
    try:
        repository.get_veto(run.id)
        has_veto = True
    except ConflictError:
        has_veto = False
    assert has_veto is False


def test_activation_uses_weakest_tool_dependency_level(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    tool = repository.create_bucket(name="browser-tool", kind="tool")
    run_service, approval_service = _service(repository)
    run, bucket = _rehearsed_run(repository, run_service, level=4)
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = 1 WHERE id = ?", (tool.id,))
        connection.execute(
            "UPDATE buckets SET tool_versions_json = ? WHERE id = ?",
            ('{"browser-tool":"1"}', bucket.id),
        )

    outcome = approval_service.begin_activation(run.id)

    assert outcome.kind == "denied"
    assert outcome.run.state is RunState.DENIED


def test_activation_applies_idle_decay_before_selecting_tier(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository, policy=POLICY)
    run, bucket = _rehearsed_run(repository, run_service, level=4)
    interval = POLICY.decay_idle_days * 86_400
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE buckets SET last_activity_at = 0, last_decay_at = NULL WHERE id = ?",
            (bucket.id,),
        )
    approval_service = ApprovalService(
        run_service,
        clock=lambda: interval,
        veto_seconds=POLICY.veto_seconds,
        policy=POLICY,
    )

    outcome = approval_service.begin_activation(run.id)

    assert outcome.kind == "veto_pending"
    assert repository.get_bucket_by_id(bucket.id).level == 3


def test_l2_pauses_and_resumes_after_explicit_approval(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=2)

    outcome = approval_service.begin_activation(run.id)
    assert outcome.kind == "approval_pending"
    assert repository.get_run(run.id).state is RunState.APPROVAL_PENDING

    resolved = approval_service.resolve_approval(run.id, approved=True)
    assert resolved.state is RunState.AUTHORIZED


def test_l2_pause_rolls_back_transition_when_sdk_state_persistence_fails(tmp_path) -> None:
    fail = {"enabled": False}

    def inject(point: str) -> None:
        if fail["enabled"] and point == "before_compound_commit":
            raise RuntimeError("injected persistence failure")

    repository = Repository(tmp_path / "apprentice.db", fault_injector=inject)
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=2)
    fail["enabled"] = True

    with pytest.raises(RuntimeError, match="injected"):
        approval_service.begin_activation(run.id, sdk_state_text="resume-state")

    reloaded = repository.get_run(run.id)
    assert reloaded.state is RunState.REHEARSED
    assert reloaded.sdk_state_ref is None


def test_sdk_state_is_consumed_once_under_concurrency(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=2)
    approval_service.begin_activation(run.id, sdk_state_text="resume-state")
    approval_service.resolve_approval(run.id, approved=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(lambda _index: approval_service.pop_sdk_state(run.id), range(2)))

    assert sorted(value for value in values if value is not None) == ["resume-state"]
    assert values.count(None) == 1


def test_l2_rejection_never_authorizes(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=2)

    outcome = approval_service.begin_activation(run.id)
    assert outcome.kind == "approval_pending"

    resolved = approval_service.resolve_approval(run.id, approved=False)
    assert resolved.state is RunState.DENIED
    # Never reaches authorized/executing --- production is never touched.
    assert repository.get_run(run.id).state is RunState.DENIED


def test_l2_rejection_persists_the_supplied_reason(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, bucket = _rehearsed_run(repository, run_service, level=2)
    approval_service.begin_activation(run.id)

    resolved = approval_service.resolve_approval(
        run.id, approved=False, reason="wrong merchant"
    )

    assert resolved.state is RunState.DENIED
    events = repository.list_events(bucket.id, run_id=run.id)
    denial_event = next(event for event in events if event.evidence_key == f"approval:{run.id}")
    assert denial_event.detail["reason"] == "wrong merchant"


def test_l3_creates_exactly_one_veto_countdown_for_the_whole_plan(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository, now=1_000.0, veto_seconds=5.0)
    run, _bucket = _rehearsed_run(repository, run_service, level=3)

    outcome = approval_service.begin_activation(run.id)

    assert outcome.kind == "veto_pending"
    assert repository.get_run(run.id).state is RunState.VETO_PENDING
    veto = repository.get_veto(run.id)
    assert veto.deadline == 1_005.0
    assert veto.status is VetoStatus.PENDING

    # A second activation attempt for the same run is rejected outright (the
    # run is no longer rehearsed) and must not create a second countdown ---
    # one veto row per plan, not one per action or per attempt.
    try:
        approval_service.begin_activation(run.id)
        second_attempt_rejected = False
    except NotRehearsedError:
        second_attempt_rejected = True
    assert second_attempt_rejected
    with repository.db.transaction() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM vetoes WHERE run_id = ?", (run.id,)
        ).fetchone()[0]
    assert count == 1


def test_l3_begin_activation_atomically_creates_transition_and_veto_row(tmp_path) -> None:
    """A crash between the run's ``veto_pending`` transition and its veto row must be
    impossible: a forced failure inside that single repository transaction must
    leave neither half committed, never a run stuck in ``veto_pending`` with no
    veto row to resolve it.
    """
    fail = {"enabled": False}

    def inject(point: str) -> None:
        if fail["enabled"] and point == "before_compound_commit":
            raise RuntimeError("injected transaction failure")

    repository = Repository(tmp_path / "apprentice.db", fault_injector=inject)
    run_service, approval_service = _service(repository, now=1_000.0, veto_seconds=5.0)
    run, _bucket = _rehearsed_run(repository, run_service, level=3)
    fail["enabled"] = True

    try:
        approval_service.begin_activation(run.id)
        raised = False
    except RuntimeError:
        raised = True
    assert raised

    assert repository.get_run(run.id).state is RunState.REHEARSED
    try:
        repository.get_veto(run.id)
        has_veto = True
    except ConflictError:
        has_veto = False
    assert has_veto is False


def test_l3_cancellation_prevents_authorization(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository, now=1_000.0, veto_seconds=5.0)
    run, _bucket = _rehearsed_run(repository, run_service, level=3)
    approval_service.begin_activation(run.id)

    resolved = approval_service.cancel_veto(run.id)

    assert resolved.state is RunState.VETOED
    assert repository.get_veto(run.id).status is VetoStatus.VETOED


def test_l3_expiry_authorizes_after_the_deadline_using_the_injected_clock(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    times = [1_000.0]
    run_service = RunService(repository)
    approval_service = ApprovalService(
        run_service, clock=lambda: times[0], veto_seconds=5.0, id_factory=_counting_ids()
    )
    run, _bucket = _rehearsed_run(repository, run_service, level=3)
    approval_service.begin_activation(run.id)

    # Not yet due.
    times[0] = 1_004.0
    assert approval_service.resolve_veto_expiry(run.id) is None
    assert repository.get_run(run.id).state is RunState.VETO_PENDING

    # Deadline reached.
    times[0] = 1_005.0
    resolved = approval_service.resolve_veto_expiry(run.id)
    assert resolved is not None
    assert resolved.state is RunState.AUTHORIZED
    assert repository.get_veto(run.id).status is VetoStatus.EXPIRED


def test_repeated_expiry_resolution_executes_at_most_once(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    times = [1_000.0]
    run_service = RunService(repository)
    approval_service = ApprovalService(
        run_service, clock=lambda: times[0], veto_seconds=5.0, id_factory=_counting_ids()
    )
    run, _bucket = _rehearsed_run(repository, run_service, level=3)
    approval_service.begin_activation(run.id)
    times[0] = 1_010.0

    first = approval_service.resolve_veto_expiry(run.id)
    second = approval_service.resolve_veto_expiry(run.id)

    assert first is not None
    assert second is None
    assert repository.get_run(run.id).state is RunState.AUTHORIZED


def test_repeated_cancel_after_expiry_raises_rather_than_double_resolving(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    times = [1_000.0]
    run_service = RunService(repository)
    approval_service = ApprovalService(
        run_service, clock=lambda: times[0], veto_seconds=5.0, id_factory=_counting_ids()
    )
    run, _bucket = _rehearsed_run(repository, run_service, level=3)
    approval_service.begin_activation(run.id)
    times[0] = 1_010.0
    approval_service.resolve_veto_expiry(run.id)

    try:
        approval_service.cancel_veto(run.id)
        raised = False
    except VetoAlreadyResolvedError:
        raised = True
    assert raised
    assert repository.get_run(run.id).state is RunState.AUTHORIZED


def test_veto_pending_survives_a_new_service_instance_on_the_same_database(tmp_path) -> None:
    db_path = tmp_path / "apprentice.db"
    repository_a = Repository(db_path)
    run_service_a = RunService(repository_a)
    approval_service_a = ApprovalService(
        run_service_a, clock=lambda: 1_000.0, veto_seconds=5.0, id_factory=_counting_ids()
    )
    run, _bucket = _rehearsed_run(repository_a, run_service_a, level=3)
    approval_service_a.begin_activation(run.id)

    # Simulate a full process restart: brand-new Repository/RunService/ApprovalService
    # instances opened against the same database file.
    repository_b = Repository(db_path)
    run_service_b = RunService(repository_b)
    approval_service_b = ApprovalService(
        run_service_b, clock=lambda: 1_006.0, veto_seconds=5.0, id_factory=_counting_ids()
    )

    assert repository_b.get_run(run.id).state is RunState.VETO_PENDING
    assert repository_b.get_veto(run.id).status is VetoStatus.PENDING

    resolved = approval_service_b.resolve_veto_expiry(run.id)
    assert resolved is not None
    assert resolved.state is RunState.AUTHORIZED


def test_l4_authorizes_immediately(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=4)

    outcome = approval_service.begin_activation(run.id)

    assert outcome.kind == "authorized"
    assert isinstance(outcome, ApprovalOutcome)
    assert repository.get_run(run.id).state is RunState.AUTHORIZED


def test_http_activation_and_approval_boundary(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository)
    approval_service = ApprovalService(run_service, clock=lambda: 1_000.0, veto_seconds=5.0)
    run, _bucket = _rehearsed_run(repository, run_service, level=2)
    client = TestClient(
        build_sidecar(
            repository=repository,
            run_service=run_service,
            approval_service=approval_service,
        )
    )

    activated = client.post(f"/api/runs/{run.id}/activate")
    assert activated.status_code == 200
    assert activated.json()["kind"] == "approval_pending"
    assert repository.get_run(run.id).state is RunState.APPROVAL_PENDING

    approved = client.post(f"/api/runs/{run.id}/approval", json={"approved": True})
    assert approved.status_code == 200
    assert approved.json()["state"] == "authorized"


def test_http_cannot_activate_a_created_run(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository)
    client = TestClient(build_sidecar(repository=repository, run_service=run_service))
    bucket = repository.create_bucket(name="file-expense")
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    run_service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    run = run_service.create_run(bucket.name, {"amount": "1.00"})

    response = client.post(f"/api/runs/{run.id}/activate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_rehearsed"


def test_http_veto_cancel(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository)
    approval_service = ApprovalService(run_service, clock=lambda: 1_000.0, veto_seconds=5.0)
    run, _bucket = _rehearsed_run(repository, run_service, level=3)
    client = TestClient(
        build_sidecar(
            repository=repository,
            run_service=run_service,
            approval_service=approval_service,
        )
    )
    client.post(f"/api/runs/{run.id}/activate")

    response = client.post(f"/api/runs/{run.id}/veto")

    assert response.status_code == 200
    assert response.json()["state"] == "vetoed"


def test_sdk_approval_state_round_trips_through_the_approval_boundary_and_resumes(
    tmp_path,
) -> None:
    """L2's full HITL loop: persisted across the pending gap, resumed after approval.

    Exercises ``ApprovalService`` together with the isolated ``SDKStateStore``
    adapter (not a stand-in): the SDK's own resumable ``RunState`` --- the
    same shape ``Runner.run`` produces the instant the model calls a
    ``needs_approval`` tool --- is serialized, persisted through
    ``runs.sdk_state_ref``, survives the L2 pending gap, and is resumed
    (approved) only after a human decision, never before.
    """
    repository = Repository(tmp_path / "apprentice.db")
    run_service, approval_service = _service(repository)
    run, _bucket = _rehearsed_run(repository, run_service, level=2)

    agent = _production_agent()
    store = SDKStateStore(agent)
    pending_state = build_pending_interruption_state(
        agent, run_id=run.id, tool_name="activate_rehearsed_plan", call_id="call-1"
    )

    outcome = approval_service.begin_activation(
        run.id, sdk_state_text=store.serialize(pending_state)
    )
    assert outcome.kind == "approval_pending"
    # Persisted, not held only in memory: a fresh read from the run row.
    assert repository.get_run(run.id).sdk_state_ref is not None

    approval_service.resolve_approval(run.id, approved=True)
    resumed_text = approval_service.pop_sdk_state(run.id)
    assert resumed_text is not None
    # Consumed exactly once: a second pop finds nothing left to resume from.
    assert approval_service.pop_sdk_state(run.id) is None

    restored_state = store.deserialize(resumed_text)
    interruption = restored_state.get_interruptions()[0]
    assert interruption.tool_name == "activate_rehearsed_plan"
    restored_state.approve(interruption)
    approvals = restored_state._context._approvals["activate_rehearsed_plan"]
    assert approvals.approved == [interruption.raw_item.call_id]


def test_http_activation_persists_sdk_state_that_resumes_after_restart(tmp_path) -> None:
    """The real HTTP surface (not just the unit seam) persists resumable SDK state.

    An L2 activation through POST /activate must leave a serialized SDK
    ``RunState`` in ``runs.sdk_state_ref`` so that a brand-new app instance
    on the same database can approve the run and resume the very same SDK
    run state --- the durable approval boundary the plan requires.
    """
    db_path = tmp_path / "apprentice.db"
    repository_a = Repository(db_path)
    run_service_a = RunService(repository_a)
    run, _bucket = _rehearsed_run(repository_a, run_service_a, level=2)
    client_a = TestClient(build_sidecar(repository=repository_a, run_service=run_service_a))

    activated = client_a.post(f"/api/runs/{run.id}/activate")
    assert activated.status_code == 200
    assert activated.json()["kind"] == "approval_pending"
    assert repository_a.get_run(run.id).sdk_state_ref is not None

    # Full restart: new Repository/RunService/ApprovalService/app on the same file.
    repository_b = Repository(db_path)
    run_service_b = RunService(repository_b)
    approval_service_b = ApprovalService(run_service_b, veto_seconds=5.0)
    client_b = TestClient(
        build_sidecar(
            repository=repository_b,
            run_service=run_service_b,
            approval_service=approval_service_b,
        )
    )
    assert repository_b.get_run(run.id).state is RunState.APPROVAL_PENDING

    approved = client_b.post(f"/api/runs/{run.id}/approval", json={"approved": True})
    assert approved.status_code == 200
    assert approved.json()["state"] == "authorized"

    resumed_text = approval_service_b.pop_sdk_state(run.id)
    assert resumed_text is not None
    store = SDKStateStore(_production_agent())
    restored_state = store.deserialize(resumed_text)
    interruption = restored_state.get_interruptions()[0]
    assert interruption.tool_name == "activate_rehearsed_plan"
    restored_state.approve(interruption)
