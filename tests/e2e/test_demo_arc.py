"""End-to-end proof of Apprentice's central safety claim.

One deterministic arc walks all 16 numbered steps from the Task 8 brief:
induction, held-out shadow evidence, twin rehearsal, the L1-L4 approval
boundary, exact production replay, contract-drift detection, and automatic
demotion. Every model call is a fake, deterministic runner (no OpenAI API key
is used anywhere in this file); the demo portal, the twin's snapshot capture,
and the production replay are real (a real HTTP server, a real Playwright
browser). The arc never sleeps to wait for time: the L3 veto window is
expired by advancing an injected clock.

This file also carries the ten required adversarial assertions and the
five-case (plus regression) local eval corpus, and the secret-sentinel scan
over every retained fixture artifact.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from apprentice.canonical import digest
from apprentice.config import load_policy
from apprentice.executor.agent import TwinRunOutcome, run_twin_agent
from apprentice.executor.replay import ProductionReplayer, execute_authorized_run
from apprentice.executor.twin_tools import FileValue, TwinEnvironment
from apprentice.induction.induce import Anchor, Playbook, induce_playbook
from apprentice.induction.shadow import (
    ProposedAction,
    ShadowSubmission,
    evaluate_heldout_shadow,
    record_qualifying_shadow,
)
from apprentice.ledger.repository import Repository
from apprentice.ledger.scoring import evaluate_promotion, unique_evidence_counts
from apprentice.ledger.transitions import confirm_promotion
from apprentice.models import EventType, RecordedAction, RunState, VetoStatus
from apprentice.recorder.artifacts import assert_no_sentinels, load_artifact
from apprentice.sidecar.approval_service import ApprovalService, ChecksFailedError
from apprentice.sidecar.console import build_dashboard_context
from apprentice.sidecar.run_service import RunService
from apprentice.twin.builder import Twin, build_twin
from apprentice.twin.invariants import CHECK_NAMES, RehearsalReport, evaluate_rehearsal
from apprentice.twin.snapshot import Fetcher, SnapshotEntry, default_fetcher, host_of
from demo_portal.app import build_portal

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "expense"
POLICY_PATH = REPO_ROOT / "trust_policy.yaml"
SENTINELS = ("hunter2", "123456")
_ALL_CHECKS_PASS = dict.fromkeys(CHECK_NAMES, True)
_RECEIPT_FILENAME = "receipt.pdf"
_RECEIPT_BYTES = b"apprentice-demo-arc-fake-receipt-bytes"
_RECEIPT_SHA256 = hashlib.sha256(_RECEIPT_BYTES).hexdigest()


# --- Shared, deterministic infrastructure -----------------------------------


class FakeClock:
    """A settable clock the arc advances explicitly; never real wall-clock sleep."""

    def __init__(self, start: float) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _serve_on_port(app: Any, port: int, name: str) -> Iterator[str]:
    """Run a FastAPI app on an exact, caller-chosen port for one `with` block.

    Used twice in sequence (never concurrently) on the *same* port number so
    the "site changed" step of the arc can present a drifted contract at the
    exact same address the capability was trained and promoted against.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, name=name, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            pytest.fail(f"{name} did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        listener.close()


def _login(page: Any, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.get_by_label("Username").fill("demo")
    page.get_by_label("Password").fill("not-recorded")
    page.get_by_role("button", name="Sign in").click()


def _authenticated_fetcher(base_url: str) -> Fetcher:
    """A real, cookie-authenticated read-only fetcher: the twin's live snapshot capture."""
    session = httpx.Client()
    session.post(f"{base_url}/login", data={"username": "demo", "password": "not-recorded"})

    def fetch(method: str, url: str) -> SnapshotEntry:
        response = session.request(method, url)
        return SnapshotEntry(
            method=method, url=url, status=response.status_code,
            headers=dict(response.headers), body=response.text,
        )

    return fetch


def _static_fetcher(body: str) -> Fetcher:
    def fetcher(method: str, url: str) -> SnapshotEntry:
        return SnapshotEntry(method=method, url=url, status=200, headers={}, body=body)

    return fetcher


def _file_resolver_returning(data: bytes):
    def resolver(_filename: str, _sha256: str) -> bytes:
        return data

    return resolver


def _rewrite_fixture_host(fixture: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Rewrite a canonically recorded fixture's origin to a real running target's address."""
    return json.loads(json.dumps(fixture).replace("http://127.0.0.1/", f"{base_url}/"))


def _inputs(*, merchant: str, amount: str, justification: str = "") -> dict[str, Any]:
    return {
        "merchant": merchant,
        "amount": amount,
        "justification": justification,
        "receipt": {"filename": _RECEIPT_FILENAME, "sha256": _RECEIPT_SHA256},
    }


def _fake_twin_runner(env: TwinEnvironment, *, merchant: str, amount: str, justification: str = ""):
    """Drives ``TwinEnvironment`` directly --- exactly what a model calling
    the twin's function tools would cause to happen --- so no rehearsal in
    this file ever requires an OpenAI API key."""

    def runner(agent: Any, prompt: str) -> str:
        env.navigate("/expense")
        env.fill(Anchor(css="#merchant", role="textbox", name="Merchant"), merchant)
        env.fill(Anchor(css="#amount", role="spinbutton", name="Amount"), amount)
        env.fill(
            Anchor(
                css="#justification", role="textbox",
                name="Justification for expenses over 1000",
            ),
            justification,
        )
        env.upload(
            Anchor(css="#receipt", role="button", name="Receipt"),
            FileValue(filename=_RECEIPT_FILENAME, sha256=_RECEIPT_SHA256),
        )
        env.commit(Anchor(css="button", role="button", name="Submit expense"))
        return "done"

    return runner


def _expected_playbook() -> Playbook:
    raw = json.loads((FIXTURES / "expected_playbook.json").read_text(encoding="utf-8"))
    return Playbook.model_validate(raw)


def _fake_induction_runner(expected: Playbook):
    def runner(agent: Any, prompt: str) -> Playbook:
        return expected

    return runner


def _eval_cases() -> dict[str, Any]:
    return json.loads((FIXTURES / "eval_cases.json").read_text(encoding="utf-8"))


def _shadow_submission_from_case(case: dict[str, Any]) -> ShadowSubmission:
    submission = case["submission"]
    return ShadowSubmission(
        actions=tuple(
            ProposedAction(
                action=action["action"],
                anchor=Anchor.model_validate(action["anchor"]),
                value=action["value"],
            )
            for action in submission["actions"]
        ),
        branch_taken=submission["branch_taken"],
        success_status=submission["success_status"],
        success_text=submission["success_text"],
    )


def _fake_shadow_runner(submission: ShadowSubmission):
    def runner(agent: Any, prompt: str) -> ShadowSubmission:
        return submission

    return runner


def _run_twin_rehearsal(
    *,
    fixture: dict[str, Any],
    playbook: Playbook,
    allowed_hosts: Sequence[str],
    inputs: dict[str, Any],
    fill: dict[str, str],
    fetcher: Fetcher = default_fetcher,
    snapshot_urls: tuple[str, ...] | None = (),
    target_base_url: str = "http://127.0.0.1",
) -> tuple[TwinRunOutcome, RehearsalReport, Twin]:
    twin = build_twin(
        fixture, allowed_hosts=allowed_hosts, fetcher=fetcher, snapshot_urls=snapshot_urls
    )
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url=target_base_url)
    outcome = run_twin_agent(
        env, playbook, inputs,
        runner=_fake_twin_runner(
            env,
            merchant=fill["merchant"],
            amount=fill["amount"],
            justification=fill.get("justification", ""),
        ),
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )
    return outcome, report, twin


def _lightweight_actions() -> list[RecordedAction]:
    """A minimal, hand-authored two-step plan for tests that exercise the
    approval/replay boundary itself rather than the twin."""

    navigate = RecordedAction(
        ordinal=0, tool_name="navigate", arguments={"path": "/expense"},
        arguments_digest=digest({"path": "/expense"}), effect="observe",
    )
    commit_arguments = {
        "anchor": {"css": "button", "role": "button", "name": "Submit expense"},
        "payload_digest": "deadbeef",
        "method": "POST",
        "url": "http://127.0.0.1/expense",
    }
    commit = RecordedAction(
        ordinal=1, tool_name="commit", arguments=commit_arguments,
        arguments_digest=digest(commit_arguments), effect="commit",
    )
    return [navigate, commit]


def _lightweight_rehearsed_run(
    repository: Repository,
    run_service: RunService,
    *,
    name: str,
    level: int,
    checks: dict[str, bool] | None = None,
    actions: list[RecordedAction] | None = None,
):
    """Build a rehearsed run without a real twin or portal: for tests of the
    approval/veto/replay machinery itself, not of rehearsal."""

    bucket = repository.create_bucket(name=name)
    repository.add_demonstration(bucket.id, "training-1", f"{name}-t1", "training")
    repository.add_demonstration(bucket.id, "training-2", f"{name}-t2", "training")
    run_service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = ? WHERE id = ?", (level, bucket.id))
    run = run_service.create_run(bucket.name, {"amount": "42.00"})
    run_service.start_rehearsal(run.id)
    updated_run, _event = run_service.record_rehearsal_result(
        run.id,
        passed=True,
        checks=dict(checks) if checks is not None else dict(_ALL_CHECKS_PASS),
        trace_ref="twin-run-1",
        actions=actions if actions is not None else _lightweight_actions(),
    )
    return repository.get_bucket_by_id(bucket.id), updated_run


# --- The automated demo arc: all 16 numbered steps --------------------------


def test_demo_arc_end_to_end(tmp_path: Path) -> None:
    """All 16 numbered steps of the Task 8 brief, fully automated and deterministic.

    Also carries adversarial assertions: duplicate held-out evidence does not
    promote further (step 3), and a failed rehearsal creates zero production
    mutation (steps 14-15).
    """
    clock = FakeClock(start=1_720_000_000.0)
    policy = load_policy(POLICY_PATH)
    repository = Repository(tmp_path / "apprentice.db", clock=clock)
    run_service = RunService(repository, clock=clock, policy=policy)
    approval_service = ApprovalService(run_service, clock=clock, veto_seconds=policy.veto_seconds)

    # --- 1. Load two training demonstrations. ---
    training_1 = load_artifact(FIXTURES / "training_1")
    training_2 = load_artifact(FIXTURES / "training_2")
    heldout = load_artifact(FIXTURES / "heldout")

    # --- 2. Induce and validate Playbook v1 with a fake deterministic model
    # response in CI. ---
    expected_playbook = _expected_playbook()
    playbook = induce_playbook(
        {"training_1": training_1, "training_2": training_2},
        runner=_fake_induction_runner(expected_playbook),
    )
    assert playbook == expected_playbook

    port = _free_port()
    with _serve_on_port(build_portal("none"), port, "demo-arc-portal") as base_url:
        bucket = repository.create_bucket(name="file-expense", target_base_url=base_url)
        repository.add_demonstration(
            bucket.id, "training_1", training_1["artifact_digest"], "training"
        )
        repository.add_demonstration(
            bucket.id, "training_2", training_2["artifact_digest"], "training"
        )
        repository.add_demonstration(bucket.id, "heldout", heldout["artifact_digest"], "heldout")
        activated = run_service.activate_capability(
            bucket.id, reviewed_playbook=playbook.model_dump(mode="json")
        )
        assert activated.level == 1
        assert activated.activated_at is not None

        # --- 3. Run one unique held-out shadow evaluation. ---
        heldout_case = next(
            case for case in _eval_cases()["cases"] if case["id"] == "heldout_happy_path"
        )
        shadow_submission = _shadow_submission_from_case(heldout_case)
        shadow_event = record_qualifying_shadow(
            run_service, bucket_id=bucket.id, playbook_version=1, playbook=playbook,
            heldout_artifact=heldout, runner=_fake_shadow_runner(shadow_submission),
        )
        assert shadow_event.type is EventType.SHADOW_PASS

        # Adversarial: duplicate held-out evidence does not promote (further).
        duplicate_event = record_qualifying_shadow(
            run_service, bucket_id=bucket.id, playbook_version=1, playbook=playbook,
            heldout_artifact=heldout, runner=_fake_shadow_runner(shadow_submission),
        )
        assert duplicate_event.id == shadow_event.id
        events_after_duplicate = repository.list_events(bucket.id)
        assert unique_evidence_counts(events_after_duplicate)["shadow_pass"] == 1

        # --- 4. Confirm L2 eligibility and promote. ---
        evaluation = evaluate_promotion(
            repository.get_bucket_by_id(bucket.id), events_after_duplicate, policy,
            training_demonstrations=2, now=clock(),
        )
        assert evaluation.eligible is True
        assert evaluation.target_level == 2
        bucket = confirm_promotion(repository, bucket.id, policy, now=clock())
        assert bucket.level == 2

        rewritten = _rewrite_fixture_host(training_1, base_url)
        fetcher = _authenticated_fetcher(base_url)
        live_host = host_of(base_url)

        # --- 5. Create a new RunSpec with new merchant/amount. ---
        inputs_1 = _inputs(merchant="Contoso Retail", amount="152.00")
        run_1 = run_service.create_run(bucket.name, inputs_1)
        run_service.start_rehearsal(run_1.id)

        # --- 6. Capture the current read-only snapshot. --- (a real, live,
        # authenticated GET of /expense, merged over the recorded fixture)
        # --- 7. Rehearse in the twin and store the action plan. ---
        outcome_1, report_1, _twin_1 = _run_twin_rehearsal(
            fixture=rewritten, playbook=playbook, allowed_hosts=[live_host], inputs=inputs_1,
            fill={"merchant": "Contoso Retail", "amount": "152.00"}, fetcher=fetcher,
            snapshot_urls=(f"{base_url}/expense",), target_base_url=base_url,
        )
        assert report_1.passed is True
        run_1, _rehearsal_event_1 = run_service.record_rehearsal_result(
            run_1.id, passed=report_1.passed, checks=report_1.checks,
            trace_ref="twin-run-1", actions=outcome_1.actions,
        )
        assert run_1.state is RunState.REHEARSED

        # --- 8. Confirm the production mutation counter is still zero. ---
        assert httpx.get(f"{base_url}/api/debug/mutations").json() == {"mutation_count": 0}

        # --- 9. Resolve the L2 approval and replay production. ---
        activation_1 = approval_service.begin_activation(run_1.id)
        assert activation_1.kind == "approval_pending"
        approval_service.resolve_approval(run_1.id, approved=True)
        updated_run_1, replay_outcome_1 = execute_authorized_run(
            run_service, run_1.id, file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
            authenticate=lambda page: _login(page, base_url),
        )

        # --- 10. Confirm the counter is exactly one and the run succeeds. ---
        assert replay_outcome_1.succeeded is True
        assert updated_run_1.state is RunState.SUCCEEDED
        assert httpx.get(f"{base_url}/api/debug/mutations").json() == {"mutation_count": 1}

        # --- 11. Complete a second approved success and promote to L3. ---
        inputs_2 = _inputs(merchant="Fabrikam Supplies", amount="210.50")
        run_2 = run_service.create_run(bucket.name, inputs_2)
        run_service.start_rehearsal(run_2.id)
        outcome_2, report_2, _twin_2 = _run_twin_rehearsal(
            fixture=rewritten, playbook=playbook, allowed_hosts=[live_host], inputs=inputs_2,
            fill={"merchant": "Fabrikam Supplies", "amount": "210.50"}, fetcher=fetcher,
            snapshot_urls=(f"{base_url}/expense",), target_base_url=base_url,
        )
        assert report_2.passed is True
        run_service.record_rehearsal_result(
            run_2.id, passed=report_2.passed, checks=report_2.checks,
            trace_ref="twin-run-2", actions=outcome_2.actions,
        )
        activation_2 = approval_service.begin_activation(run_2.id)
        assert activation_2.kind == "approval_pending"
        approval_service.resolve_approval(run_2.id, approved=True)
        _updated_run_2, replay_outcome_2 = execute_authorized_run(
            run_service, run_2.id, file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
            authenticate=lambda page: _login(page, base_url),
        )
        assert replay_outcome_2.succeeded is True
        assert httpx.get(f"{base_url}/api/debug/mutations").json() == {"mutation_count": 2}

        events_after_second_success = repository.list_events(bucket.id)
        promotion_to_3 = evaluate_promotion(
            repository.get_bucket_by_id(bucket.id), events_after_second_success, policy,
            training_demonstrations=2, now=clock(),
        )
        assert promotion_to_3.eligible is True
        assert promotion_to_3.target_level == 3
        bucket = confirm_promotion(repository, bucket.id, policy, now=clock())
        assert bucket.level == 3

        # --- 12. Start an L3 run, let the injected veto clock expire, and
        # execute once. ---
        inputs_3 = _inputs(merchant="Globex Hardware", amount="64.10")
        run_3 = run_service.create_run(bucket.name, inputs_3)
        run_service.start_rehearsal(run_3.id)
        outcome_3, report_3, _twin_3 = _run_twin_rehearsal(
            fixture=rewritten, playbook=playbook, allowed_hosts=[live_host], inputs=inputs_3,
            fill={"merchant": "Globex Hardware", "amount": "64.10"}, fetcher=fetcher,
            snapshot_urls=(f"{base_url}/expense",), target_base_url=base_url,
        )
        assert report_3.passed is True
        run_service.record_rehearsal_result(
            run_3.id, passed=report_3.passed, checks=report_3.checks,
            trace_ref="twin-run-3", actions=outcome_3.actions,
        )
        activation_3 = approval_service.begin_activation(run_3.id)
        assert activation_3.kind == "veto_pending"
        assert activation_3.veto_deadline == clock() + policy.veto_seconds
        clock.advance(policy.veto_seconds)  # the injected clock alone expires the window
        resolved_3 = approval_service.resolve_veto_expiry(run_3.id)
        assert resolved_3 is not None
        assert resolved_3.state is RunState.AUTHORIZED
        _updated_run_3, replay_outcome_3 = execute_authorized_run(
            run_service, run_3.id, file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
            authenticate=lambda page: _login(page, base_url),
        )
        assert replay_outcome_3.succeeded is True
        assert httpx.get(f"{base_url}/api/debug/mutations").json() == {"mutation_count": 3}

    # portal instance "none" has stopped here (the `with` block exited).

    # --- 13. Switch to a meaningful changed-site mode. --- (the exact same
    # address, so this is the *same* capability watching its own target drift)
    with _serve_on_port(build_portal("commit_schema"), port, "demo-arc-portal-changed") as changed:
        assert changed == base_url
        assert httpx.get(f"{changed}/api/debug/mutations").json() == {"mutation_count": 0}

        inputs_4 = _inputs(merchant="Initech Office Supply", amount="41.00")
        run_4 = run_service.create_run(bucket.name, inputs_4)
        run_service.start_rehearsal(run_4.id)
        outcome_4, report_4, _twin_4 = _run_twin_rehearsal(
            fixture=rewritten, playbook=playbook, allowed_hosts=[host_of(changed)],
            inputs=inputs_4, fill={"merchant": "Initech Office Supply", "amount": "41.00"},
            fetcher=_authenticated_fetcher(changed), snapshot_urls=(f"{changed}/expense",),
            target_base_url=changed,
        )

        # --- 14. Rehearsal fails. ---
        assert report_4.passed is False
        assert report_4.checks["commit_payload_matches_inputs"] is False
        run_4, _event_4 = run_service.record_rehearsal_result(
            run_4.id, passed=report_4.passed, checks=report_4.checks,
            trace_ref="twin-run-4", actions=outcome_4.actions,
        )
        assert run_4.state is RunState.REHEARSAL_FAILED

        # --- 15. Production mutation counter remains unchanged. ---
        # Adversarial: a failed rehearsal creates zero production mutation.
        assert httpx.get(f"{changed}/api/debug/mutations").json() == {"mutation_count": 0}

        # --- 16. Capability automatically demotes with a visible reason. ---
        demoted_bucket = repository.get_bucket_by_id(bucket.id)
        assert demoted_bucket.level == 2  # a major-severity failure demotes exactly one level
        dashboard = build_dashboard_context(repository, policy, now=clock())
        card = next(c for c in dashboard["capabilities"] if c["bucket_id"] == bucket.id)
        assert card["level"] == 2
        assert card["latest_demotion_cause"] is not None
        assert "major" in card["latest_demotion_cause"]

    # Adversarial: all retained artifacts pass the secret sentinel scan.
    assert_no_sentinels(FIXTURES, SENTINELS)


# --- Adversarial assertions not already covered above -----------------------


def test_forged_rehearsal_claims_are_rejected(tmp_path: Path) -> None:
    """Adversarial: tampered stored rehearsal evidence is rejected, not activated."""
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository)
    approval_service = ApprovalService(run_service, veto_seconds=5.0)

    _bucket, run = _lightweight_rehearsed_run(
        repository, run_service, name="forged-checks", level=2
    )
    failing_checks = {**_ALL_CHECKS_PASS, "no_undeclared_hosts": False}
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE rehearsals SET checks_json = ? WHERE run_id = ?",
            (json.dumps(failing_checks, sort_keys=True), run.id),
        )
    with pytest.raises(ChecksFailedError):
        approval_service.begin_activation(run.id)
    assert repository.get_run(run.id).state is RunState.REHEARSAL_FAILED

    _bucket2, run2 = _lightweight_rehearsed_run(
        repository, run_service, name="forged-hash", level=2
    )
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE run_actions SET arguments_digest = 'tampered' WHERE run_id = ? AND ordinal = 0",
            (run2.id,),
        )
    with pytest.raises(ChecksFailedError):
        approval_service.begin_activation(run2.id)
    assert repository.get_run(run2.id).state is RunState.REHEARSAL_FAILED


def test_replay_action_mismatch_fails(tmp_path: Path) -> None:
    """Adversarial: a stored plan tampered with after authorization never replays."""
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository)
    _bucket, run = _lightweight_rehearsed_run(
        repository, run_service, name="replay-mismatch", level=4
    )
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE run_actions SET arguments_digest = 'tampered' WHERE run_id = ? AND ordinal = 0",
            (run.id,),
        )
    # Bypasses ApprovalService (which would already catch this at the
    # boundary) to prove ProductionReplayer's own hash check is independent
    # defense-in-depth, not the only line of defense.
    run_service.authorize_from_rehearsed(run.id)
    run_service.start_execution(run.id)

    replayer = ProductionReplayer(
        repository, run.id, file_resolver=_file_resolver_returning(_RECEIPT_BYTES)
    )
    outcome = replayer.replay()

    assert outcome.succeeded is False
    assert outcome.abort_reason == "action_plan_hash_mismatch"


def test_canceled_veto_never_executes(tmp_path: Path) -> None:
    """Adversarial: canceling an L3 veto before its deadline never authorizes or executes."""
    repository = Repository(tmp_path / "apprentice.db")
    clock = FakeClock(start=2_000.0)
    run_service = RunService(repository, clock=clock)
    approval_service = ApprovalService(run_service, clock=clock, veto_seconds=5.0)
    _bucket, run = _lightweight_rehearsed_run(
        repository, run_service, name="veto-cancel", level=3
    )

    outcome = approval_service.begin_activation(run.id)
    assert outcome.kind == "veto_pending"
    resolved = approval_service.cancel_veto(run.id)
    assert resolved.state is RunState.VETOED

    clock.advance(10.0)  # well past the original deadline; never sleeps
    assert approval_service.resolve_veto_expiry(run.id) is None
    assert repository.get_run(run.id).state is RunState.VETOED


def test_restarted_veto_state_remains_pending_and_resolvable(tmp_path: Path) -> None:
    """Adversarial: a pending L3 veto survives a full process restart and still resolves."""
    db_path = tmp_path / "apprentice.db"
    clock_a = FakeClock(start=3_000.0)
    repository_a = Repository(db_path, clock=clock_a)
    run_service_a = RunService(repository_a, clock=clock_a)
    approval_service_a = ApprovalService(run_service_a, clock=clock_a, veto_seconds=5.0)
    _bucket, run = _lightweight_rehearsed_run(
        repository_a, run_service_a, name="veto-restart", level=3
    )
    approval_service_a.begin_activation(run.id)
    assert repository_a.get_run(run.id).state is RunState.VETO_PENDING

    # Simulate a full process restart: brand-new objects on the same db file.
    clock_b = FakeClock(start=3_010.0)
    repository_b = Repository(db_path, clock=clock_b)
    run_service_b = RunService(repository_b, clock=clock_b)
    approval_service_b = ApprovalService(run_service_b, clock=clock_b, veto_seconds=5.0)

    assert repository_b.get_run(run.id).state is RunState.VETO_PENDING
    assert repository_b.get_veto(run.id).status is VetoStatus.PENDING
    resolved = approval_service_b.resolve_veto_expiry(run.id)
    assert resolved is not None
    assert resolved.state is RunState.AUTHORIZED


def test_unknown_critical_response_fails() -> None:
    """Adversarial: an unmatched mutation response is never improvised as a success."""
    fixture = dict(load_artifact(FIXTURES / "training_1"))
    fixture["response_templates"] = []
    playbook = _expected_playbook()
    inputs = _inputs(merchant="Northwind Books", amount="87.25")

    outcome, report, twin = _run_twin_rehearsal(
        fixture=fixture, playbook=playbook, allowed_hosts=["127.0.0.1"], inputs=inputs,
        fill={"merchant": "Northwind Books", "amount": "87.25"},
    )

    assert outcome.aborted_reason == "unknown_mutation_template"
    assert twin.router.captured_mutations == []
    assert report.passed is False
    assert report.checks["success_criteria_met"] is False


# --- The five-case (plus regression) local eval corpus ----------------------


def test_eval_corpus_matches_expected_facts() -> None:
    """Run after every prompt change: the five required cases plus regressions.

    Covers required adversarial assertions "Unknown hosts fail" (case
    ``unexpected_external_host``) and "Wrong commit amount fails" (case
    ``wrong_amount_commit_payload``).
    """
    data = _eval_cases()
    playbook = _expected_playbook()

    for case in data["cases"]:
        _assert_playbook_facts(playbook, case["expected_playbook_facts"])
        fixture = load_artifact(FIXTURES / case["artifact"])
        submission = _shadow_submission_from_case(case)
        result = evaluate_heldout_shadow(
            playbook, fixture, runner=_fake_shadow_runner(submission)
        )
        assert result.passed is case["expected_pass"], case["id"]
        assert sorted(result.mismatches) == sorted(
            case["expected_invariant_outcomes"]["mismatches"]
        ), case["id"]
        facts = case["expected_action_plan_facts"]
        assert len(submission.actions) == facts["step_count"], case["id"]
        assert submission.actions[-1].action == facts["final_action"], case["id"]

    for case in data["rehearsal_cases"]:
        _assert_playbook_facts(playbook, case["expected_playbook_facts"])
        fixture = load_artifact(FIXTURES / case["artifact"])
        rehearsal = case["rehearsal"]
        fetcher = default_fetcher
        snapshot_urls: tuple[str, ...] | None = ()
        if rehearsal["portal_failure_mode"]:
            client = TestClient(build_portal(rehearsal["portal_failure_mode"]))
            client.post(
                "/login", data={"username": "demo", "password": "not-recorded"},
                follow_redirects=False,
            )
            fetcher = _static_fetcher(client.get("/expense").text)
            snapshot_urls = None  # fall back to the fixture's own recorded GET urls

        rehearsal_inputs = {
            **rehearsal["inputs"],
            "receipt": {"filename": _RECEIPT_FILENAME, "sha256": _RECEIPT_SHA256},
        }
        outcome, report, _twin = _run_twin_rehearsal(
            fixture=fixture, playbook=playbook, allowed_hosts=rehearsal["allowed_hosts"],
            inputs=rehearsal_inputs, fill=rehearsal["fill"], fetcher=fetcher,
            snapshot_urls=snapshot_urls,
        )
        assert report.passed is case["expected_pass"], case["id"]
        failing = set(case["expected_invariant_outcomes"]["failing_checks"])
        for name in CHECK_NAMES:
            assert report.checks[name] is (name not in failing), f"{case['id']}: {name}"
        facts = case["expected_action_plan_facts"]
        assert len(outcome.actions) == facts["step_count"], case["id"]
        if facts["final_action"] is not None:
            assert outcome.actions[-1].tool_name == facts["final_action"], case["id"]


def _assert_playbook_facts(playbook: Playbook, facts: dict[str, Any]) -> None:
    assert playbook.task == facts["task"]
    assert playbook.success_criteria.status_code == facts["success_status_code"]
    assert playbook.success_criteria.page_contains == facts["success_page_contains"]
    if "decision_point_condition" in facts:
        decision = playbook.decision_points[0]
        assert decision.condition == facts["decision_point_condition"]
        assert decision.activates_input == facts["decision_point_activates_input"]


# --- Secret sentinel scan over every retained artifact -----------------------


def test_all_retained_fixture_artifacts_pass_sentinel_scan() -> None:
    """Adversarial: every retained artifact under fixtures/expense/** is secret-free."""
    assert_no_sentinels(FIXTURES, SENTINELS)
