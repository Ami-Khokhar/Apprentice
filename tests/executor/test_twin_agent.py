from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
from agents.exceptions import MaxTurnsExceeded

from apprentice.executor.agent import run_twin_agent
from apprentice.executor.twin_tools import FileValue, TwinEnvironment, parse_page
from apprentice.induction.induce import Anchor, Playbook
from apprentice.ledger.repository import Repository
from apprentice.recorder.artifacts import load_artifact
from apprentice.sidecar.run_service import RunService
from apprentice.twin.builder import build_twin
from apprentice.twin.invariants import evaluate_rehearsal
from apprentice.twin.responses import TwinAbortError
from apprentice.twin.snapshot import SnapshotEntry
from demo_portal.app import build_portal

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"
_RECEIPT_SHA = "78d34c0be3938936c1ea608f35cc9eb85e663b9e3e1954c2e48a18a412580810"

_NAVIGATE_ANCHOR = Anchor(css="main", role="document", name="File an expense")
_MERCHANT_ANCHOR = Anchor(css="#merchant", role="textbox", name="Merchant")
_AMOUNT_ANCHOR = Anchor(css="#amount", role="spinbutton", name="Amount")
_JUSTIFICATION_ANCHOR = Anchor(
    css="#justification", role="textbox", name="Justification for expenses over 1000"
)
_RECEIPT_ANCHOR = Anchor(css="#receipt", role="button", name="Receipt")
_COMMIT_ANCHOR = Anchor(css="button", role="button", name="Submit expense")


def test_twin_anchor_resolution_rejects_a_role_change() -> None:
    page = parse_page(
        "<html><title>Expense</title><main><a id='submit'>Submit expense</a></main></html>"
    )
    anchor = Anchor(css="#submit", role="button", name="Submit expense")

    with pytest.raises(TwinAbortError, match="anchor_role_mismatch"):
        page.resolve(anchor)


def _expected_playbook() -> Playbook:
    raw = json.loads((FIXTURES / "expected_playbook.json").read_text(encoding="utf-8"))
    return Playbook.model_validate(raw)


def _fake_runner(env: TwinEnvironment, *, merchant: str, amount: str, justification: str = ""):
    def runner(agent, prompt):
        env.navigate("/expense")
        env.fill(_MERCHANT_ANCHOR, merchant)
        env.fill(_AMOUNT_ANCHOR, amount)
        env.fill(_JUSTIFICATION_ANCHOR, justification)
        env.upload(_RECEIPT_ANCHOR, FileValue(filename="receipt.pdf", sha256=_RECEIPT_SHA))
        env.commit(_COMMIT_ANCHOR)
        return "done"

    return runner


def _happy_inputs(*, merchant: str = "Northwind Books", amount: str = "87.25") -> dict:
    return {
        "merchant": merchant,
        "amount": amount,
        "justification": "",
        "receipt": {"filename": "receipt.pdf", "sha256": _RECEIPT_SHA},
    }


def _static_fetcher(body: str):
    def fetcher(method: str, url: str) -> SnapshotEntry:
        return SnapshotEntry(method=method, url=url, status=200, headers={}, body=body)

    return fetcher


def test_normal_expense_rehearsal_passes() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="87.25")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert outcome.aborted_reason is None
    assert len(outcome.actions) == len(playbook.steps)
    assert report.passed is True
    assert report.checks == dict.fromkeys(report.checks, True)


def test_twin_agent_fails_closed_when_model_exceeds_turn_limit() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")

    def looping_runner(agent, prompt):
        raise MaxTurnsExceeded("looping model")

    outcome = run_twin_agent(env, playbook, _happy_inputs(), runner=looping_runner)

    assert outcome.aborted_reason == "max_turns_exceeded"
    assert outcome.actions == ()


def test_wrong_amount_fails_commit_payload_matches_inputs() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    # The declared input says 87.25, but the (mis-behaving) run fills 99.99.
    inputs = _happy_inputs(amount="87.25")

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="99.99")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert outcome.aborted_reason is None
    assert report.passed is False
    assert report.checks["commit_payload_matches_inputs"] is False


def test_rename_amount_fails_before_mutation() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    with _renamed_amount_client() as client:
        client.post(
            "/login", data={"username": "demo", "password": "not-recorded"}, follow_redirects=False
        )
        renamed_html = client.get("/expense").text

    twin = build_twin(
        fixture, allowed_hosts=["127.0.0.1"], fetcher=_static_fetcher(renamed_html)
    )
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="87.25")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert outcome.aborted_reason == "anchor_name_mismatch"
    assert twin.router.captured_mutations == []
    assert report.passed is False
    assert report.checks["action_plan_matches_playbook"] is False


def _renamed_amount_client():
    from fastapi.testclient import TestClient

    return TestClient(build_portal("rename_amount"))


def test_commit_schema_fails_payload_validation() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    changed = load_artifact(FIXTURES / "changed_site")
    changed_body = changed["http_entries"][0]["response"]["body"]
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], fetcher=_static_fetcher(changed_body))
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="87.25")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert outcome.aborted_reason is None
    assert len(twin.router.captured_mutations) == 1
    assert "amount" not in twin.router.captured_mutations[0].body["fields"]
    assert report.passed is False
    assert report.checks["commit_payload_matches_inputs"] is False


def test_request_to_an_unseen_host_fails() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    # allowed_hosts deliberately excludes the fixture's own recorded host.
    twin = build_twin(fixture, allowed_hosts=["example.invalid"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="87.25")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert outcome.aborted_reason == "unseen_host"
    assert twin.router.captured_mutations == []
    assert report.passed is False


def test_unknown_mutation_response_template_fails_rather_than_inventing_success() -> None:
    fixture = dict(load_artifact(FIXTURES / "training_1"))
    fixture["response_templates"] = []
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="87.25")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert outcome.aborted_reason == "unknown_mutation_template"
    assert twin.router.captured_mutations == []
    assert report.passed is False
    assert report.checks["success_criteria_met"] is False


@contextmanager
def _trap_server() -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """A live HTTP server that records every request it receives.

    Unlike the demo portal's mutation counter (which only increments for an
    *authenticated* valid POST, so a forwarded-but-unauthenticated mutation
    would not move it), this trap registers any contact at all --- exactly
    what is needed to detect a forwarding regression in capture_mutation.
    """

    requests: list[tuple[str, str]] = []

    class TrapHandler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            requests.append((self.command, self.path))
            body = b"<html><main><h1>Expense submitted</h1></main></html>"
            self.send_response(201)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _record

        def log_message(self, *_args: object) -> None:  # keep test output pristine
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
    thread = Thread(target=server.serve_forever, name="twin-trap", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_trap_http_server_receives_zero_twin_mutations() -> None:
    """If capture_mutation ever forwarded the mutating request, the trap WOULD record it."""

    playbook = _expected_playbook()
    inputs = _happy_inputs()
    with _trap_server() as (base_url, trap_requests):
        # The twin targets the live trap itself: its fixture URLs, allowed
        # hosts, and target_base_url all point at the trap's real host:port,
        # so a forwarded request has nowhere to go *except* the trap.
        raw = json.dumps(load_artifact(FIXTURES / "training_1"))
        fixture = json.loads(raw.replace("http://127.0.0.1/", f"{base_url}/"))
        trap_host = base_url.removeprefix("http://")
        twin = build_twin(fixture, allowed_hosts=[trap_host], snapshot_urls=())
        env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url=base_url)

        outcome = run_twin_agent(
            env,
            playbook,
            inputs,
            runner=_fake_runner(env, merchant="Northwind Books", amount="87.25"),
        )
        report = evaluate_rehearsal(
            playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
        )

        # Not vacuous: the rehearsal genuinely reached and recorded its commit
        # against the trap's own host, and passed every check.
        assert outcome.aborted_reason is None
        assert [action.tool_name for action in outcome.actions][-1] == "commit"
        assert len(twin.router.captured_mutations) == 1
        assert twin.router.captured_mutations[0].url == f"{base_url}/expense"
        assert report.passed is True

        # The property under test: the twin never contacted the trap at all.
        assert trap_requests == []

        # Control probe: a genuinely forwarded request WOULD have been recorded.
        control = httpx.post(f"{base_url}/expense", data={"amount": "87.25"})
        assert control.status_code == 201
        assert trap_requests == [("POST", "/expense")]


def test_action_plan_and_hash_are_stable() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    inputs = _happy_inputs()

    def run_once():
        twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
        env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
        return run_twin_agent(
            env,
            playbook,
            inputs,
            runner=_fake_runner(env, merchant="Northwind Books", amount="87.25"),
        )

    first = run_once()
    second = run_once()

    assert first.action_plan_hash == second.action_plan_hash
    assert [action.model_dump(mode="json") for action in first.actions] == [
        action.model_dump(mode="json") for action in second.actions
    ]

    third_twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    third_env = TwinEnvironment(
        playbook=playbook, twin=third_twin, target_base_url="http://127.0.0.1"
    )
    third = run_twin_agent(
        third_env,
        playbook,
        inputs,
        runner=_fake_runner(third_env, merchant="Northwind Books", amount="12.00"),
    )
    assert third.action_plan_hash != first.action_plan_hash


def test_a_passing_rehearsal_is_stored_as_an_immutable_action_plan(tmp_path) -> None:
    """The full pipeline: agent run -> invariants -> one persisted transaction."""

    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(
        env, playbook, inputs, runner=_fake_runner(env, merchant="Northwind Books", amount="87.25")
    )
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )
    assert report.passed is True

    repository = Repository(tmp_path / "apprentice.db")
    bucket = repository.create_bucket(name="file-expense")
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    service = RunService(repository)
    service.activate_capability(bucket.id, reviewed_playbook=playbook.model_dump(mode="json"))
    run = service.create_run("file-expense", inputs)
    service.start_rehearsal(run.id)

    updated_run, event = service.record_rehearsal_result(
        run.id,
        passed=report.passed,
        checks=report.checks,
        trace_ref="twin-run-1",
        actions=outcome.actions,
    )

    assert updated_run.state.value == "rehearsed"
    assert updated_run.action_plan_hash == outcome.action_plan_hash
    assert event.type.value == "twin_pass"

    with repository.db.transaction() as connection:
        stored_actions = connection.execute(
            "SELECT tool_name, effect, status FROM run_actions WHERE run_id = ? ORDER BY ordinal",
            (run.id,),
        ).fetchall()
    assert [row["tool_name"] for row in stored_actions] == [
        step.action for step in playbook.steps
    ]
    assert all(row["status"] == "planned" for row in stored_actions)


@pytest.mark.live
def test_live_gpt_5_6_completes_the_expense_task_inside_the_twin() -> None:
    """Exercises the real Agents SDK call against GPT-5.6 driving the twin tools.

    Requires credentials for APPRENTICE_PROVIDER and network access; excluded from
    the default test run (see pytest markers in pyproject.toml). Run
    explicitly with `uv run pytest -m live` and inspect the resulting Agents
    SDK trace before tuning the twin agent's instructions.
    """

    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    inputs = _happy_inputs()

    outcome = run_twin_agent(env, playbook, inputs)
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )

    assert report.passed is True, json.dumps(
        {
            "aborted_reason": outcome.aborted_reason,
            "actions": [action.model_dump(mode="json") for action in outcome.actions],
            "checks": report.checks,
        },
        indent=2,
    )
