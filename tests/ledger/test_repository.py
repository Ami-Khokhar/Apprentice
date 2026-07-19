from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import BaseModel

from apprentice.canonical import (
    InvalidBaseUrlError,
    canonical_json,
    digest,
    normalize_base_url,
)
from apprentice.config import load_policy
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.ledger.transitions import confirm_promotion
from apprentice.models import (
    EventType,
    FailureCause,
    RecordedAction,
    RunState,
    Severity,
    VetoStatus,
)
from apprentice.sidecar.run_service import RunService
from apprentice.twin.invariants import CHECK_NAMES

ALL_CHECKS_PASS = dict.fromkeys(CHECK_NAMES, True)


def activate_bucket(
    repository: Repository,
    bucket,
    *,
    playbook: dict | None = None,
):
    repository.add_demonstration(bucket.id, "training-1", f"{bucket.id}-training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", f"{bucket.id}-training-2", "training")
    return RunService(repository).activate_capability(
        bucket.id,
        reviewed_playbook=playbook or {"task": bucket.name, "version": 1},
    )


def set_level_for_test(repository: Repository, bucket_id: int, level: int) -> None:
    with repository.db.transaction(write=True) as connection:
        cursor = connection.execute("UPDATE buckets SET level = ? WHERE id = ?", (level, bucket_id))
    assert cursor.rowcount == 1


def test_canonical_digest_is_independent_of_mapping_key_order() -> None:
    first = {"merchant": "Café", "details": {"amount": 42, "currency": "INR"}}
    second = {"details": {"currency": "INR", "amount": 42}, "merchant": "Café"}

    assert canonical_json(first) == canonical_json(second)
    assert digest(first) == digest(second)
    assert canonical_json(first) == (
        b'{"details":{"amount":42,"currency":"INR"},"merchant":"Caf\xc3\xa9"}'
    )


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), {1: "non-string-key"}, {"unsupported": {1, 2}}],
)
def test_canonical_json_rejects_non_json_or_ambiguous_values(value) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json(value)


def test_canonical_json_accepts_pydantic_models_without_python_repr() -> None:
    class Input(BaseModel):
        merchant: str
        amount: int

    assert canonical_json(Input(merchant="Café", amount=42)) == (
        b'{"amount":42,"merchant":"Caf\xc3\xa9"}'
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://./",
        "http://-bad.example",
        "http://bad_.example",
        "http://example..com",
        "http://999.999.999.999",
        "http://example.com:",
        "http://example.com:abc",
        "http://example.com:70000",
        "http://[::1]:",
    ],
)
def test_base_url_rejects_empty_invalid_hosts_and_malformed_ports(url: str) -> None:
    with pytest.raises(InvalidBaseUrlError):
        normalize_base_url(url)


def test_base_url_normalizes_dns_and_ipv6_hosts() -> None:
    assert normalize_base_url("HTTP://EXAMPLE.COM.:80/root/") == (
        "http://example.com/root",
        "example.com",
    )
    assert normalize_base_url("https://[2001:0DB8::1]:443/") == (
        "https://[2001:db8::1]",
        "2001:db8::1",
    )


def test_repository_records_demonstrations_with_unique_artifact_digests(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")

    demonstration = repository.add_demonstration(
        bucket.id,
        artifact_ref="fixtures/expense/training_1/artifact.json",
        artifact_digest="trace-digest-1",
        role="training",
    )
    duplicate = repository.add_demonstration(
        bucket.id,
        artifact_ref="fixtures/expense/training_1/artifact.json",
        artifact_digest="trace-digest-1",
        role="training",
    )

    assert duplicate == demonstration
    assert repository.count_demonstrations(bucket.id, role="training") == 1


def test_bucket_creation_and_run_service_have_no_trust_bypass_overrides(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")

    with pytest.raises(TypeError, match="initial_level"):
        repository.create_bucket(name="forged", initial_level=4)
    with pytest.raises(TypeError, match="reviewed_playbook"):
        repository.create_bucket(name="forged", reviewed_playbook={"task": "skip demonstrations"})
    with pytest.raises(TypeError, match="severity_demotion"):
        RunService(repository, severity_demotion={"major": -100})


def test_self_authored_tool_registration_is_hash_pinned_at_l1(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db", clock=lambda: 123.0)
    artifact_digest = "a" * 64

    tool = repository.register_self_authored_tool(
        "lookup-expense-policy", artifact_digest=artifact_digest
    )

    assert tool.kind.value == "tool"
    assert tool.origin.value == "self_authored"
    assert tool.level == 1
    assert tool.playbook_digest == artifact_digest
    assert tool.activated_at == 123.0
    assert repository.count_demonstrations(tool.id) == 0


def test_self_authored_tool_registration_requires_sha256(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        repository.register_self_authored_tool(
            "lookup-expense-policy", artifact_digest="not-a-sha256"
        )

    assert repository.list_buckets() == []


def test_playbook_dependency_is_pinned_to_reviewed_tool_artifact(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    playbook = activate_bucket(repository, repository.create_bucket(name="file-expense"))
    artifact_digest = "b" * 64
    service = RunService(repository)
    tool = service.register_self_authored_tool(
        "lookup-expense-policy", artifact_digest=artifact_digest
    )

    updated = service.declare_tool_dependency(
        playbook.id,
        tool_name=tool.name,
        artifact_digest=artifact_digest,
    )

    assert updated.tool_versions == {tool.name: artifact_digest}
    assert updated.toolset_digest == digest({tool.name: artifact_digest})
    assert repository.declare_tool_dependency(
        playbook.id, tool_name=tool.name, artifact_digest=artifact_digest
    ) == updated

    with pytest.raises(ConflictError, match="artifact digest mismatch"):
        repository.declare_tool_dependency(
            playbook.id, tool_name=tool.name, artifact_digest="c" * 64
        )


def test_duplicate_evidence_key_is_counted_and_demoted_only_once(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = activate_bucket(repository, repository.create_bucket(name="file-expense"))
    set_level_for_test(repository, bucket.id, 4)
    service = RunService(repository)

    first = service.record_audit_result(
        bucket_id=bucket.id,
        audit_id="run-123",
        passed=False,
        failure_cause=FailureCause.PLAN_MISMATCH,
        detail={"reason": "commit mismatch"},
    )
    duplicate = service.record_audit_result(
        bucket_id=bucket.id,
        audit_id="run-123",
        passed=False,
        failure_cause=FailureCause.PLAN_MISMATCH,
        detail={"reason": "commit mismatch"},
    )

    assert duplicate == first
    assert repository.count_events(bucket.id) == 1
    assert repository.get_bucket_by_id(bucket.id).level == 3


@pytest.mark.parametrize(
    ("failure_cause", "expected_level"),
    [(FailureCause.PLAN_MISMATCH, 3), (FailureCause.UNSAFE_SIDE_EFFECT, 2)],
)
def test_failure_evidence_demotes_in_the_same_transaction(
    tmp_path, failure_cause: FailureCause, expected_level: int
) -> None:
    repository = Repository(tmp_path / f"{failure_cause.value}.db")
    bucket = activate_bucket(repository, repository.create_bucket(name="file-expense"))
    set_level_for_test(repository, bucket.id, 4)

    event = RunService(repository).record_audit_result(
        bucket_id=bucket.id,
        audit_id=failure_cause.value,
        passed=False,
        failure_cause=failure_cause,
        detail={"check": "outcome"},
    )

    assert event.severity.value == (
        "major" if failure_cause is FailureCause.PLAN_MISMATCH else "critical"
    )
    assert repository.get_bucket_by_id(bucket.id).level == expected_level


def test_raw_event_insertion_is_private_and_database_rejects_unknown_types(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    service = RunService(repository)

    assert not hasattr(service, "record_evidence")
    with pytest.raises(sqlite3.IntegrityError), repository.db.transaction(write=True) as connection:
        connection.execute(
            """
            INSERT INTO events (
              id, bucket_id, run_id, type, weight, severity,
              evidence_key, detail_json, created_at
            ) VALUES ('forged', ?, NULL, 'self_reported_success', 100, NULL,
                      'forged:1', '{}', 0)
            """,
            (bucket.id,),
        )

    assert repository.count_events(bucket.id) == 0


def test_repository_persists_canonical_json_not_caller_formatting(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    activate_bucket(repository, bucket, playbook={"task": "file expense", "version": 1})
    service = RunService(repository, id_factory=lambda: "run-1")
    run = service.create_run(
        "file-expense",
        {"z": 1, "a": {"two": 2, "one": 1}},
    )

    assert run.bucket_id == bucket.id
    assert run.playbook_version == 1
    assert run.input_digest == digest({"a": {"one": 1, "two": 2}, "z": 1})
    assert run.inputs == {"a": {"one": 1, "two": 2}, "z": 1}
    with repository.db.transaction() as connection:
        row = connection.execute("SELECT inputs_json FROM runs WHERE id = ?", (run.id,)).fetchone()
    assert row is not None
    assert row["inputs_json"] == '{"a":{"one":1,"two":2},"z":1}'


def test_run_snapshots_server_owned_playbook_hosts_and_tool_versions(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(
        name="file-expense",
        target_base_url="HTTP://LOCALHOST:80/expense/",
        tool_versions={"browser": "2.1.0"},
    )
    bucket = activate_bucket(repository, bucket, playbook={"task": "file expense", "version": 1})

    run = RunService(repository, id_factory=lambda: "run-1").create_run(
        "file-expense",
        {"amount": 42},
    )

    assert run.playbook_version == 1
    assert run.playbook_digest == digest({"task": "file expense", "version": 1})
    assert run.tool_versions == {"browser": "2.1.0"}
    assert run.toolset_digest == bucket.toolset_digest
    assert run.target_base_url == "http://localhost/expense"
    assert run.allowed_hosts == ("localhost",)
    stored_playbook = repository.get_reviewed_playbook(bucket.id)
    assert stored_playbook.content == {"task": "file expense", "version": 1}
    assert stored_playbook.digest == run.playbook_digest


def test_repository_rejects_in_memory_database() -> None:
    with pytest.raises(ValueError, match="file-backed"):
        Repository(":memory:")


def test_activation_requires_two_unique_training_demonstrations_and_review(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    service = RunService(repository)

    assert bucket.level == 0
    assert bucket.activated_at is None
    with pytest.raises(ConflictError, match="not activated"):
        service.create_run("file-expense", {"amount": 42})

    repository.add_demonstration(bucket.id, "trace-1", "digest-1", "training")
    with pytest.raises(ConflictError, match="two unique"):
        repository.register_reviewed_playbook(bucket.id, {"task": "file expense"}, version=2)
    with pytest.raises(ConflictError, match="two unique"):
        service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})

    repository.add_demonstration(bucket.id, "trace-2", "digest-2", "training")
    activated = service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})

    assert activated.level == 1
    assert activated.activated_at is not None
    run = service.create_run("file-expense", {"amount": 42})
    assert run.playbook_digest == digest({"task": "file expense"})


def test_reviewed_playbook_versions_are_sequential_and_reinduction_resets_l1(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = activate_bucket(
        repository,
        repository.create_bucket(name="file-expense"),
        playbook={"task": "file expense", "revision": "one"},
    )
    set_level_for_test(repository, bucket.id, 3)
    repository.add_demonstration(bucket.id, "heldout", "heldout-v1", "heldout")
    RunService(repository).record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=1,
        heldout_artifact_digest="heldout-v1",
        passed=True,
    )
    old_run = RunService(repository).create_run("file-expense", {"amount": 10})

    with pytest.raises(ConflictError, match="v2"):
        repository.register_reviewed_playbook(
            bucket.id, {"task": "file expense", "revision": "three"}, version=3
        )
    with pytest.raises(ConflictError, match="already bound"):
        repository.register_reviewed_playbook(bucket.id, {"task": "changed in place"}, version=1)

    updated = repository.register_reviewed_playbook(
        bucket.id,
        {"task": "file expense", "revision": "two"},
        version=2,
    )
    retry = repository.register_reviewed_playbook(
        bucket.id,
        {"task": "file expense", "revision": "two"},
        version=2,
    )
    new_run = RunService(repository).create_run("file-expense", {"amount": 11})

    assert retry == updated
    assert updated.level == 1
    assert updated.playbook_version == 2
    assert old_run.playbook_version == 1
    assert new_run.playbook_version == 2
    assert new_run.playbook_digest == digest({"task": "file expense", "revision": "two"})

    policy = load_policy(Path(__file__).parents[2] / "trust_policy.yaml")
    with pytest.raises(ConflictError, match="not eligible"):
        confirm_promotion(repository, bucket.id, policy, now=updated.activated_at)


def test_critical_risk_cap_is_enforced_by_storage_and_models(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = activate_bucket(
        repository,
        repository.create_bucket(name="critical-action", risk_class="critical"),
    )

    with pytest.raises(sqlite3.IntegrityError), repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = 4 WHERE id = ?", (bucket.id,))

    assert repository.get_bucket_by_id(bucket.id).level == 1


def test_returned_snapshots_are_deeply_immutable(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(
        name="file-expense",
        tool_versions={"browser": "1"},
    )
    bucket = activate_bucket(repository, bucket)
    run = RunService(repository).create_run("file-expense", {"nested": {"values": [1, 2]}})

    with pytest.raises(TypeError, match="cannot be mutated"):
        bucket.tool_versions["browser"] = "2"
    with pytest.raises(TypeError, match="cannot be mutated"):
        run.inputs["new"] = "value"
    assert run.inputs["nested"]["values"] == (1, 2)


def test_concurrent_duplicate_evidence_is_inserted_once(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    service = RunService(repository)
    barrier = Barrier(2)

    def record():
        barrier.wait()
        return service.record_audit_result(
            bucket_id=bucket.id,
            audit_id="same-audit",
            passed=True,
            detail={"check": "stable"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        events = list(executor.map(lambda _index: record(), range(2)))

    assert events[0] == events[1]
    assert repository.count_events(bucket.id) == 1


def test_idempotent_replay_with_array_valued_detail_is_not_a_false_conflict(tmp_path) -> None:
    """A reloaded event's ``detail`` deep-freezes lists to tuples (FrozenModel),
    while a service replaying the same evidence supplies a fresh dict whose
    list values are still plain lists. That must compare equal (same
    canonical JSON), not raise ``EvidenceConflictError``.
    """
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    service = RunService(repository)

    first = service.record_audit_result(
        bucket_id=bucket.id,
        audit_id="array-detail-audit",
        passed=True,
        detail={"mismatches": ["actions", "branch"]},
    )
    replay = service.record_audit_result(
        bucket_id=bucket.id,
        audit_id="array-detail-audit",
        passed=True,
        detail={"mismatches": ["actions", "branch"]},
    )

    assert replay == first
    assert repository.count_events(bucket.id) == 1


def test_idempotent_replay_with_different_array_detail_still_conflicts(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    service = RunService(repository)

    service.record_audit_result(
        bucket_id=bucket.id,
        audit_id="array-detail-audit-2",
        passed=True,
        detail={"mismatches": ["actions", "branch"]},
    )

    from apprentice.ledger.repository import EvidenceConflictError

    with pytest.raises(EvidenceConflictError):
        service.record_audit_result(
            bucket_id=bucket.id,
            audit_id="array-detail-audit-2",
            passed=True,
            detail={"mismatches": ["actions"]},
        )


def _rehearsed_run_for_veto(repository: Repository, service: RunService, *, name: str):
    bucket = activate_bucket(repository, repository.create_bucket(name=name))
    run = service.create_run(name, {"amount": "1.00"})
    service.start_rehearsal(run.id)
    navigate = RecordedAction(
        ordinal=0,
        tool_name="navigate",
        arguments={"path": "/expense"},
        arguments_digest=digest({"path": "/expense"}),
        effect="observe",
    )
    commit = RecordedAction(
        ordinal=1,
        tool_name="commit",
        arguments={"payload_digest": "abc"},
        arguments_digest=digest({"payload_digest": "abc"}),
        effect="commit",
    )
    updated, _event = service.record_rehearsal_result(
        run.id,
        passed=True,
        checks=ALL_CHECKS_PASS,
        trace_ref="trace-1",
        actions=(navigate, commit),
    )
    assert updated.bucket_id == bucket.id
    return updated


def test_begin_veto_window_rolls_back_run_transition_and_veto_row_together(tmp_path) -> None:
    """A crash mid-transaction must never strand a ``veto_pending`` run with no veto row.

    Both halves --- the run's ``rehearsed`` -> ``veto_pending`` transition and
    its one countdown row --- are written in the same transaction, so either
    both land or neither does.
    """
    fail = {"enabled": False}

    def inject(point: str) -> None:
        if fail["enabled"] and point == "before_compound_commit":
            raise RuntimeError("injected transaction failure")

    repository = Repository(tmp_path / "apprentice.db", fault_injector=inject)
    service = RunService(repository)
    run = _rehearsed_run_for_veto(repository, service, name="veto-flow")
    fail["enabled"] = True

    with pytest.raises(RuntimeError, match="injected"):
        repository.begin_veto_window(
            run.id,
            veto_id="veto-1",
            deadline=123.0,
            sdk_state_text="resume-state",
            changed_at=456.0,
        )

    reloaded = repository.get_run(run.id)
    assert reloaded.state is RunState.REHEARSED
    assert reloaded.sdk_state_ref is None
    with pytest.raises(ConflictError, match="no veto countdown"):
        repository.get_veto(run.id)


def test_resolve_veto_with_event_rolls_back_veto_cas_and_run_transition_together(
    tmp_path,
) -> None:
    """A crash mid-transaction must never resolve a veto without also moving its run.

    The veto CAS and the run's terminal transition/event are written in the
    same transaction here, so a canceled/expired veto with a run stuck in
    ``veto_pending`` (or vice versa) can no longer happen.
    """
    fail = {"enabled": False}

    def inject(point: str) -> None:
        if fail["enabled"] and point == "before_compound_commit":
            raise RuntimeError("injected transaction failure")

    repository = Repository(tmp_path / "apprentice.db", fault_injector=inject)
    service = RunService(repository)
    run = _rehearsed_run_for_veto(repository, service, name="veto-flow-2")
    repository.begin_veto_window(
        run.id, veto_id="veto-2", deadline=1_000.0, sdk_state_text=None, changed_at=1.0
    )
    fail["enabled"] = True

    with pytest.raises(RuntimeError, match="injected"):
        repository.resolve_veto_with_event(
            run.id,
            from_status=VetoStatus.PENDING,
            to_status=VetoStatus.VETOED,
            resolved_at=2.0,
            expected_state=RunState.VETO_PENDING,
            new_state=RunState.VETOED,
            event_id="rollback-veto-event",
            event_type=EventType.VETOED,
            weight=1.0,
            severity=Severity.MINOR,
            evidence_key=f"veto:{run.id}",
            detail={},
            demotion_levels=0,
            created_at=2.0,
            reason="human_vetoed",
        )

    assert repository.get_veto(run.id).status is VetoStatus.PENDING
    assert repository.get_run(run.id).state is RunState.VETO_PENDING
    # Only the rehearsal's twin_pass event exists --- no vetoed event leaked through.
    assert repository.count_events(run.bucket_id) == 1


def test_only_a_persisted_heldout_trace_can_create_shadow_evidence(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    repository.add_demonstration(bucket.id, "training-1", "training-digest", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-digest-2", "training")
    repository.add_demonstration(bucket.id, "heldout", "heldout-digest", "heldout")
    service = RunService(repository)
    service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})

    with pytest.raises(ConflictError, match="held-out"):
        service.record_shadow_result(
            bucket_id=bucket.id,
            playbook_version=1,
            heldout_artifact_digest="training-digest",
            passed=True,
        )

    first = service.record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=1,
        heldout_artifact_digest="heldout-digest",
        passed=True,
    )
    duplicate = service.record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=1,
        heldout_artifact_digest="heldout-digest",
        passed=True,
    )

    assert duplicate == first
    assert repository.count_events(bucket.id) == 1
