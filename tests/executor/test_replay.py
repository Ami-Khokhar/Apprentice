"""Exact deterministic production replay against a real, running demo_portal.

Every happy-path and abort scenario here drives a real headless Chromium
page over a real HTTP server (no fakes): the only injected seam is
``file_resolver``, exactly as the plan calls for (raw file bytes come from
outside this harness). Route interception is real Playwright network
interception, not a stand-in.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page

from apprentice.canonical import digest
from apprentice.executor.agent import build_production_agent, default_activation
from apprentice.executor.replay import (
    ProductionReplayer,
    ReplayAbortError,
    _parse_multipart,
    execute_authorized_run,
)
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.models import EventType, RecordedAction, RunState, Severity
from apprentice.sidecar.run_service import RunService
from apprentice.twin.invariants import CHECK_NAMES
from demo_portal.app import build_portal

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"
_ALL_CHECKS_PASS = dict.fromkeys(CHECK_NAMES, True)
_RECEIPT_BYTES = b"%PDF-1.4 fake receipt bytes for replay tests"
_RECEIPT_SHA256 = hashlib.sha256(_RECEIPT_BYTES).hexdigest()
_RECEIPT_FILENAME = "receipt.pdf"
_RECEIPT_CONTENT_TYPE = "application/pdf"


@dataclass(frozen=True)
class _RunningPortal:
    base_url: str


@contextmanager
def _serve(app, name: str) -> Iterator[str]:
    """Run any FastAPI app on a free local port for the duration of one test."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name=name,
            daemon=True,
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


@pytest.fixture
def commit_schema_portal() -> Iterator[_RunningPortal]:
    """A demo_portal instance running the ``commit_schema`` drift failure mode."""

    with _serve(build_portal("commit_schema"), "commit-schema-portal") as base_url:
        yield _RunningPortal(base_url=base_url)


@pytest.fixture
def rename_amount_portal() -> Iterator[_RunningPortal]:
    """A demo_portal instance running the ``rename_amount`` label-drift failure mode."""

    with _serve(build_portal("rename_amount"), "rename-amount-portal") as base_url:
        yield _RunningPortal(base_url=base_url)


_BEACON_PAGE = """<!doctype html>
<html lang="en">
  <head><title>File an expense</title></head>
  <body>
    <main>
      <h1>File an expense</h1>
      <form method="post" action="/expense" enctype="multipart/form-data">
        <label for="merchant">Merchant</label>
        <input id="merchant" name="merchant">
      </form>
      <script>
        // A synchronous mutating request fired during page load: exactly the
        // kind of live-target behavior the replayer must never forward.
        var beacon = new XMLHttpRequest();
        beacon.open("POST", "/expense", false);
        try { beacon.send("beacon=1"); } catch (error) {}
      </script>
    </main>
  </body>
</html>"""


def _build_beacon_app():
    """A live target whose expense page fires a POST during load (no click needed)."""

    from fastapi import FastAPI, Response
    from fastapi.responses import HTMLResponse

    app = FastAPI()
    app.state.post_hits = 0

    @app.get("/expense", response_class=HTMLResponse)
    async def expense_page() -> HTMLResponse:
        return HTMLResponse(_BEACON_PAGE)

    @app.post("/expense")
    async def expense_post() -> Response:
        app.state.post_hits += 1
        return Response(status_code=201)

    return app


@pytest.fixture
def beacon_portal() -> Iterator[tuple[_RunningPortal, Any]]:
    app = _build_beacon_app()
    with _serve(app, "beacon-portal") as base_url:
        yield _RunningPortal(base_url=base_url), app


_CROSS_HOST_GET_PAGE = """<!doctype html>
<html lang="en">
  <head><title>File an expense</title></head>
  <body>
    <main>
      <h1>File an expense</h1>
      <img src="http://localhost:{port}/pixel" alt="" />
    </main>
  </body>
</html>"""


def _build_cross_host_get_app(port_holder: list[int]):
    """A live target whose expense page loads an image from a second hostname.

    ``localhost`` and ``127.0.0.1`` both resolve to the loopback interface
    (so if this GET is ever forwarded, this same process observes the hit),
    but they are textually different hosts --- exactly the ``allowed_hosts``
    mismatch the replay guard must reject regardless of HTTP method.
    ``port_holder`` is filled in by the fixture once the OS-assigned port is
    known, since the page body is rendered before that.
    """

    from fastapi import FastAPI, Response
    from fastapi.responses import HTMLResponse

    app = FastAPI()
    app.state.get_hits = 0

    @app.get("/expense", response_class=HTMLResponse)
    async def expense_page() -> HTMLResponse:
        return HTMLResponse(_CROSS_HOST_GET_PAGE.format(port=port_holder[0]))

    @app.get("/pixel")
    async def pixel() -> Response:
        app.state.get_hits += 1
        return Response(status_code=204)

    return app


@pytest.fixture
def cross_host_get_portal() -> Iterator[tuple[_RunningPortal, Any]]:
    port_holder = [0]
    app = _build_cross_host_get_app(port_holder)
    with _serve(app, "cross-host-get-portal") as base_url:
        port_holder[0] = urlsplit(base_url).port
        yield _RunningPortal(base_url=base_url), app


def _cross_host_actions(base_url: str) -> list[RecordedAction]:
    """A minimal navigate+commit plan: the abort must happen during navigate,
    before the commit action (and any anchor it references) is ever touched.
    """
    navigate_anchor = {"css": "main", "role": "document", "name": "File an expense"}
    navigate_arguments = {"path": "/expense", "anchor": navigate_anchor}
    commit_arguments = {
        "anchor": {"css": "button", "role": "button", "name": "Submit expense"},
        "method": "POST",
        "url": urljoin(f"{base_url}/", "expense"),
        "payload_digest": digest({"kind": "multipart", "fields": {}, "files": []}),
    }
    return [
        RecordedAction(
            ordinal=0,
            tool_name="navigate",
            arguments=navigate_arguments,
            arguments_digest=digest(navigate_arguments),
            effect="observe",
        ),
        RecordedAction(
            ordinal=1,
            tool_name="commit",
            arguments=commit_arguments,
            arguments_digest=digest(commit_arguments),
            effect="commit",
        ),
    ]


def _login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.get_by_label("Username").fill("demo")
    page.get_by_label("Password").fill("not-recorded")
    page.get_by_role("button", name="Sign in").click()


def _file_resolver_returning(data: bytes):
    def resolver(_filename: str, _sha256: str) -> bytes:
        return data

    return resolver


def _commit_body(*, merchant: str, amount: str, justification: str, amount_field: str = "amount"):
    return {
        "kind": "multipart",
        "fields": {"merchant": merchant, amount_field: amount, "justification": justification},
        "files": [
            {
                "field": "receipt",
                "filename": _RECEIPT_FILENAME,
                "content_type": _RECEIPT_CONTENT_TYPE,
                "sha256": _RECEIPT_SHA256,
            }
        ],
    }


def _actions(
    *,
    base_url: str,
    merchant: str = "Northwind Books",
    amount: str = "87.25",
    justification: str = "",
    commit_amount_field: str = "amount",
) -> list[RecordedAction]:
    def action(ordinal, tool_name, arguments, effect):
        return RecordedAction(
            ordinal=ordinal,
            tool_name=tool_name,
            arguments=arguments,
            arguments_digest=digest(arguments),
            effect=effect,
        )

    commit_url = urljoin(f"{base_url}/", "expense")
    body = _commit_body(
        merchant=merchant,
        amount=amount,
        justification=justification,
        amount_field=commit_amount_field,
    )
    navigate_anchor = {"css": "main", "role": "document", "name": "File an expense"}
    merchant_anchor = {"css": "#merchant", "role": "textbox", "name": "Merchant"}
    amount_anchor = {"css": "#amount", "role": "spinbutton", "name": "Amount"}
    return [
        action(0, "navigate", {"path": "/expense", "anchor": navigate_anchor}, "observe"),
        action(1, "fill", {"anchor": merchant_anchor, "value": merchant}, "prepare"),
        action(2, "fill", {"anchor": amount_anchor, "value": amount}, "prepare"),
        action(
            3,
            "fill",
            {
                "anchor": {
                    "css": "#justification",
                    "role": "textbox",
                    "name": "Justification for expenses over 1000",
                },
                "value": justification,
            },
            "prepare",
        ),
        action(
            4,
            "upload",
            {
                "anchor": {"css": "#receipt", "role": "button", "name": "Receipt"},
                "value": {
                    "filename": _RECEIPT_FILENAME,
                    "sha256": _RECEIPT_SHA256,
                    "content_type": _RECEIPT_CONTENT_TYPE,
                },
            },
            "prepare",
        ),
        action(
            5,
            "commit",
            {
                "anchor": {"css": "button", "role": "button", "name": "Submit expense"},
                "method": "POST",
                "url": commit_url,
                "payload_digest": digest(body),
            },
            "commit",
        ),
    ]


def _expected_playbook_dict() -> dict:
    return json.loads((FIXTURES / "expected_playbook.json").read_text(encoding="utf-8"))


def _authorized_run(
    tmp_path,
    *,
    base_url: str,
    actions: list[RecordedAction],
    inputs: dict | None = None,
):
    repository = Repository(tmp_path / "apprentice.db")
    run_service = RunService(repository)
    bucket = repository.create_bucket(name="file-expense", target_base_url=base_url)
    repository.add_demonstration(bucket.id, "training-1", "training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", "training-2", "training")
    run_service.activate_capability(bucket.id, reviewed_playbook=_expected_playbook_dict())
    run = run_service.create_run("file-expense", inputs or {"amount": "87.25"})
    run_service.start_rehearsal(run.id)
    run_service.record_rehearsal_result(
        run.id, passed=True, checks=_ALL_CHECKS_PASS, trace_ref="twin-run-1", actions=actions
    )
    run_service.authorize_from_rehearsed(run.id)
    return repository, run_service, run


def _action_statuses(repository: Repository, run_id: str) -> list[str]:
    with repository.db.transaction() as connection:
        rows = connection.execute(
            "SELECT status FROM run_actions WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()
    return [row["status"] for row in rows]


def test_normal_replay_increments_mutation_count_exactly_once(portal_server, tmp_path) -> None:
    actions = _actions(base_url=portal_server.base_url)
    repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal_server.base_url, actions=actions
    )

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
        authenticate=lambda page: _login(page, portal_server.base_url),
    )

    assert outcome.succeeded is True
    assert outcome.mutation_forwarded is True
    assert outcome.final_status == 201
    assert updated_run.state is RunState.SUCCEEDED
    # Every stored action was consumed exactly once, and its consumption persisted.
    assert _action_statuses(repository, run.id) == ["succeeded"] * len(actions)

    response = httpx.get(f"{portal_server.base_url}/api/debug/mutations")
    assert response.json() == {"mutation_count": 1}


def test_file_hash_mismatch_aborts_before_commit_and_demotes(portal_server, tmp_path) -> None:
    actions = _actions(base_url=portal_server.base_url)
    repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal_server.base_url, actions=actions
    )
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = 3 WHERE id = ?", (run.bucket_id,))

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=_file_resolver_returning(b"not the recorded receipt bytes at all"),
        authenticate=lambda page: _login(page, portal_server.base_url),
    )

    assert outcome.succeeded is False
    assert outcome.mutation_forwarded is False
    assert outcome.abort_reason == "file_hash_mismatch"
    assert updated_run.state is RunState.FAILED

    response = httpx.get(f"{portal_server.base_url}/api/debug/mutations")
    assert response.json() == {"mutation_count": 0}
    assert repository.get_bucket_by_id(run.bucket_id).level == 2


def test_unexpected_file_resolution_error_records_a_terminal_failure(
    portal_server, tmp_path
) -> None:
    actions = _actions(base_url=portal_server.base_url)
    _repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal_server.base_url, actions=actions
    )

    def fail_resolution(_filename: str, _sha256: str) -> bytes:
        raise OSError("storage unavailable")

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=fail_resolution,
        authenticate=lambda page: _login(page, portal_server.base_url),
    )

    assert updated_run.state is RunState.FAILED
    assert outcome.failure_cause is not None
    assert outcome.abort_reason == "unexpected_OSError"


def test_duplicate_multipart_fields_fail_closed() -> None:
    content_type = "multipart/form-data; boundary=boundary"
    body = (
        b"--boundary\r\nContent-Disposition: form-data; name=\"amount\"\r\n\r\n"
        b"999.00\r\n--boundary\r\nContent-Disposition: form-data; name=\"amount\"\r\n\r\n"
        b"42.00\r\n--boundary--\r\n"
    )

    with pytest.raises(ReplayAbortError, match="duplicate_multipart_field"):
        _parse_multipart(content_type, body)


def test_drifted_commit_schema_aborts_before_forwarding(commit_schema_portal, tmp_path) -> None:
    """A live target whose field name silently drifted must never be forwarded to."""
    actions = _actions(base_url=commit_schema_portal.base_url)
    _repository, run_service, run = _authorized_run(
        tmp_path, base_url=commit_schema_portal.base_url, actions=actions
    )

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
        authenticate=lambda page: _login(page, commit_schema_portal.base_url),
    )

    assert outcome.succeeded is False
    assert outcome.mutation_forwarded is False
    assert outcome.abort_reason == "commit_mismatch"
    assert updated_run.state is RunState.FAILED

    response = httpx.get(f"{commit_schema_portal.base_url}/api/debug/mutations")
    assert response.json() == {"mutation_count": 0}


def test_action_plan_hash_mismatch_aborts_before_any_browser_launch(
    portal_server, tmp_path
) -> None:
    actions = _actions(base_url=portal_server.base_url)
    repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal_server.base_url, actions=actions
    )
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE run_actions SET arguments_digest = 'tampered' WHERE run_id = ? AND ordinal = 0",
            (run.id,),
        )
    run_service.start_execution(run.id)

    replayer = ProductionReplayer(
        repository, run.id, file_resolver=_file_resolver_returning(_RECEIPT_BYTES)
    )
    outcome = replayer.replay()

    assert outcome.succeeded is False
    assert outcome.abort_reason == "action_plan_hash_mismatch"


def test_replay_requires_an_executing_run(portal_server, tmp_path) -> None:
    actions = _actions(base_url=portal_server.base_url)
    repository, _run_service, run = _authorized_run(
        tmp_path, base_url=portal_server.base_url, actions=actions
    )
    # authorized, but never transitioned to executing.
    replayer = ProductionReplayer(
        repository, run.id, file_resolver=_file_resolver_returning(_RECEIPT_BYTES)
    )

    try:
        replayer.replay()
        raised = False
    except ConflictError:
        raised = True
    assert raised


def test_build_production_agent_wires_a_single_needs_approval_tool() -> None:
    agent = build_production_agent(lambda run_id: f"activated {run_id}")

    assert [tool.name for tool in agent.tools] == ["activate_rehearsed_plan"]
    assert agent.tools[0].needs_approval is True


def test_anchor_drift_on_a_fill_step_aborts_and_records_a_major_failure(
    rename_amount_portal, tmp_path
) -> None:
    """A live label rename must abort cleanly: failed run, major failure, demotion, no mutation.

    The plan was rehearsed against the normal form (Amount label); the live
    target renamed it (Reimbursement total), so the fill step's accessibility
    anchor no longer resolves. This must go through the abort path --- never
    escape as a raw Playwright error leaving the run stuck in executing.
    """
    actions = _actions(base_url=rename_amount_portal.base_url)
    repository, run_service, run = _authorized_run(
        tmp_path, base_url=rename_amount_portal.base_url, actions=actions
    )
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = 3 WHERE id = ?", (run.bucket_id,))

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
        authenticate=lambda page: _login(page, rename_amount_portal.base_url),
    )

    assert outcome.succeeded is False
    assert outcome.mutation_forwarded is False
    assert outcome.abort_reason == "anchor_not_found"
    assert updated_run.state is RunState.FAILED
    # The drifted fill (ordinal 2: Amount) failed; nothing after it was consumed.
    assert _action_statuses(repository, run.id) == [
        "succeeded",
        "succeeded",
        "failed",
        "planned",
        "planned",
        "planned",
    ]
    # Plan mismatch is a major failure and demotes one level immediately.
    failure_events = [
        event
        for event in repository.list_events(run.bucket_id, run_id=run.id)
        if event.type is EventType.RUN_FAILURE
    ]
    assert len(failure_events) == 1
    assert failure_events[0].severity is Severity.MAJOR
    assert repository.get_bucket_by_id(run.bucket_id).level == 2

    response = httpx.get(f"{rename_amount_portal.base_url}/api/debug/mutations")
    assert response.json() == {"mutation_count": 0}


def test_a_mutating_request_during_a_non_commit_step_is_never_forwarded(
    beacon_portal, tmp_path
) -> None:
    """Interception covers the whole replay: a load-time POST beacon is blocked, not trusted."""
    portal, beacon_app = beacon_portal
    actions = _actions(base_url=portal.base_url)
    _repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal.base_url, actions=actions
    )

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
    )

    assert outcome.succeeded is False
    assert outcome.mutation_forwarded is False
    assert outcome.abort_reason == "unexpected_mutation"
    assert updated_run.state is RunState.FAILED
    # The live target itself never received the beacon POST (or any mutation).
    assert beacon_app.state.post_hits == 0


def test_a_cross_host_get_during_replay_is_never_forwarded(
    cross_host_get_portal, tmp_path
) -> None:
    """Plan invariant 5 is unqualified: unseen hosts fail closed, not only mutating ones.

    A non-mutating (GET) request to a host outside ``allowed_hosts`` must be
    aborted exactly like an unexpected mutation --- the live target itself
    never receives it, the run fails with a major-severity failure, and zero
    mutations are ever forwarded.
    """
    portal, cross_host_app = cross_host_get_portal
    actions = _cross_host_actions(portal.base_url)
    repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal.base_url, actions=actions
    )

    updated_run, outcome = execute_authorized_run(
        run_service,
        run.id,
        file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
    )

    assert outcome.succeeded is False
    assert outcome.mutation_forwarded is False
    assert outcome.abort_reason == "undeclared_host"
    assert updated_run.state is RunState.FAILED
    # The live cross-host target itself never received the GET.
    assert cross_host_app.state.get_hits == 0

    failure_events = [
        event
        for event in repository.list_events(run.bucket_id, run_id=run.id)
        if event.type is EventType.RUN_FAILURE
    ]
    assert len(failure_events) == 1
    assert failure_events[0].severity is Severity.MAJOR


def test_default_activation_replays_the_stored_plan_and_reports_the_outcome(
    portal_server, tmp_path
) -> None:
    """The tool's real body: no SDK involved, just the sidecar-owned activation callback."""
    actions = _actions(base_url=portal_server.base_url)
    _repository, run_service, run = _authorized_run(
        tmp_path, base_url=portal_server.base_url, actions=actions
    )
    activation = default_activation(
        run_service,
        file_resolver=_file_resolver_returning(_RECEIPT_BYTES),
        authenticate=lambda page: _login(page, portal_server.base_url),
    )

    result = activation(run.id)

    assert result == f"run {run.id} succeeded"
    response = httpx.get(f"{portal_server.base_url}/api/debug/mutations")
    assert response.json() == {"mutation_count": 1}
