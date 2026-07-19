"""A rehearsable, presenter-facing driver for the Apprentice demo.

Runs the same product arc the automated E2E proof exercises (see
``tests/e2e/test_demo_arc.py``): teach a task from two demonstrations, pass a
held-out shadow evaluation, promote to L2, rehearse and replay a run under
human approval, promote to L3, watch a durable veto window, then show a
changed site failing rehearsal cleanly and the capability demoting with a
visible reason on the real trust dashboard.

By default this uses a fake, deterministic model runner for every model
call --- exactly like CI --- so it runs with no OpenAI API key and produces
the same outcome every time you rehearse it. Pass ``--live`` (and set
the selected provider's API key) to drive it with a real model instead.

This script only orchestrates existing services (``Repository``,
``RunService``, ``ApprovalService``, ``build_sidecar``, the twin, and
``ProductionReplayer``) --- it does not reimplement any of them.

Usage:
    uv run python scripts/run_demo.py                 # deterministic, headed browser
    uv run python scripts/run_demo.py --headless       # no visible browser (smoke test)
    uv run python scripts/run_demo.py --live           # real configured inference provider
    uv run python scripts/run_demo.py --no-wait         # exit instead of blocking at the end
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from apprentice.config import load_policy
from apprentice.executor.agent import build_production_agent, default_activation, run_twin_agent
from apprentice.executor.replay import execute_authorized_run
from apprentice.executor.twin_tools import FileValue, TwinEnvironment
from apprentice.induction.induce import Anchor, Playbook, induce_playbook
from apprentice.induction.shadow import ProposedAction, ShadowSubmission, record_qualifying_shadow
from apprentice.ledger.repository import Repository
from apprentice.ledger.transitions import confirm_promotion
from apprentice.models import RunState
from apprentice.providers import api_key_env, provider_name
from apprentice.recorder.artifacts import load_artifact
from apprentice.sidecar.app import build_sidecar
from apprentice.sidecar.approval_service import ApprovalService
from apprentice.sidecar.run_service import RunService
from apprentice.twin.builder import build_twin
from apprentice.twin.invariants import evaluate_rehearsal
from apprentice.twin.snapshot import Fetcher, SnapshotEntry, host_of
from demo_portal.app import build_portal

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures" / "expense"
POLICY_PATH = REPO_ROOT / "trust_policy.yaml"
_RECEIPT_FILENAME = "receipt.pdf"
_RECEIPT_BYTES = b"apprentice-demo-receipt-bytes"
_RECEIPT_SHA256 = hashlib.sha256(_RECEIPT_BYTES).hexdigest()


def _say(message: str) -> None:
    print(f"\n>>> {message}")


def _wait_for_enter() -> None:
    """Block for Enter, but never crash a non-interactive rehearsal (no stdin)."""
    with suppress(EOFError):
        input()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _serve(app: Any, port: int, name: str) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, name=name, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError(f"{name} did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=5)
        listener.close()


def _login(page: Any, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.get_by_label("Username").fill("demo")
    page.get_by_label("Password").fill("not-recorded")
    page.get_by_role("button", name="Sign in").click()


def _authenticated_fetcher(base_url: str) -> Fetcher:
    session = httpx.Client()
    session.post(f"{base_url}/login", data={"username": "demo", "password": "not-recorded"})

    def fetch(method: str, url: str) -> SnapshotEntry:
        response = session.request(method, url)
        return SnapshotEntry(
            method=method, url=url, status=response.status_code,
            headers=dict(response.headers), body=response.text,
        )

    return fetch


def _rewrite_fixture_host(fixture: dict[str, Any], base_url: str) -> dict[str, Any]:
    return json.loads(json.dumps(fixture).replace("http://127.0.0.1/", f"{base_url}/"))


def _inputs(*, merchant: str, amount: str, justification: str = "") -> dict[str, Any]:
    return {
        "merchant": merchant,
        "amount": amount,
        "justification": justification,
        "receipt": {"filename": _RECEIPT_FILENAME, "sha256": _RECEIPT_SHA256},
    }


def _fake_twin_runner(env: TwinEnvironment, *, merchant: str, amount: str, justification: str = ""):
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


def _fake_shadow_submission() -> ShadowSubmission:
    case = json.loads((FIXTURES / "eval_cases.json").read_text(encoding="utf-8"))["cases"][0]
    submission = case["submission"]
    return ShadowSubmission(
        actions=tuple(
            ProposedAction(
                action=action["action"], anchor=Anchor.model_validate(action["anchor"]),
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


def _rehearse_and_store(
    *, run_service: RunService, run_id: str, fixture: dict[str, Any], playbook: Playbook,
    base_url: str, fetcher: Fetcher, inputs: dict[str, Any], merchant: str, amount: str,
    live: bool,
) -> bool:
    twin = build_twin(
        fixture, allowed_hosts=[host_of(base_url)], fetcher=fetcher,
        snapshot_urls=(f"{base_url}/expense",),
    )
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url=base_url)
    runner = None if live else _fake_twin_runner(env, merchant=merchant, amount=amount)
    outcome = run_twin_agent(env, playbook, inputs, runner=runner)
    report = evaluate_rehearsal(
        playbook=playbook, inputs=inputs, twin=twin, actions=outcome.actions
    )
    _say(f"Rehearsal checks: {report.checks}")
    run_service.record_rehearsal_result(
        run_id, passed=report.passed, checks=report.checks,
        trace_ref=f"twin-{run_id}", actions=outcome.actions,
    )
    return report.passed


def _approve_or_wait(
    approval_service: ApprovalService, repository: Repository, run_id: str, dashboard_url: str
) -> None:
    _say(
        f"Run {run_id} is approval_pending (L2). Open {dashboard_url} and click Approve, "
        "or press Enter here to approve it yourself."
    )
    _wait_for_enter()
    run = repository.get_run(run_id)
    if run.state is RunState.APPROVAL_PENDING:
        approval_service.resolve_approval(run_id, approved=True)
        _say("Approved from the terminal.")
    else:
        _say(f"Already resolved from the dashboard: {run.state.value}.")


def _watch_veto(
    approval_service: ApprovalService, run_id: str, veto_seconds: float, dashboard_url: str
) -> None:
    _say(
        f"Run {run_id} is in its L3 veto window (~{veto_seconds:g}s). "
        f"Cancel it on {dashboard_url} to see a veto, or wait for it to auto-authorize."
    )
    time.sleep(veto_seconds + 0.5)
    resolved = approval_service.resolve_veto_expiry(run_id)
    if resolved is not None:
        _say("Veto window expired without cancellation: authorized.")
    else:
        _say("The veto was canceled from the dashboard.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Use real Agents SDK calls through APPRENTICE_PROVIDER instead of fake "
        "deterministic runners (requires that provider's API key).",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run the production replay's browser headless (default: headed, for presenting).",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Exit immediately instead of blocking at the end so the dashboard stays open.",
    )
    args = parser.parse_args()

    required_key = api_key_env()
    if args.live and not os.environ.get(required_key):
        print(
            f"--live with APPRENTICE_PROVIDER={provider_name()} requires {required_key} to be set.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    with tempfile.TemporaryDirectory(prefix="apprentice-demo-") as tmp:
        db_path = Path(tmp) / "apprentice.db"
        policy = load_policy(POLICY_PATH)
        repository = Repository(db_path)
        run_service = RunService(repository, policy=policy)
        approval_service = ApprovalService(
            run_service, veto_seconds=policy.veto_seconds, policy=policy
        )

        portal_port = _free_port()
        sidecar_port = _free_port()
        # Both portal instances below share this exact port, so ``base_url``
        # (and the capability's target_base_url) is known up front and never
        # changes --- the "site changed" phase is the *same* address, only
        # the contract behind it drifts.
        base_url = f"http://127.0.0.1:{portal_port}"
        production_agent = build_production_agent(
            default_activation(
                run_service,
                file_resolver=lambda _filename, _sha256: _RECEIPT_BYTES,
                headless=args.headless,
                authenticate=lambda page: _login(page, base_url),
            )
        )
        sidecar_app = build_sidecar(
            repository=repository, run_service=run_service,
            approval_service=approval_service, production_agent=production_agent,
            policy=policy,
        )
        # The sidecar (and its dashboard) stays up for the whole demo; only
        # the portal underneath it is stopped and restarted in a different
        # mode partway through.
        with _serve(sidecar_app, sidecar_port, "demo-sidecar") as sidecar_url:
            dashboard_url = f"{sidecar_url}/console/dashboard"
            _say(f"Dashboard: {dashboard_url}")
            webbrowser.open(dashboard_url)

            with _serve(build_portal("none"), portal_port, "demo-portal") as started_base_url:
                assert started_base_url == base_url

                # --- 1-2. Teach: induce Playbook v1 from two demonstrations. ---
                training_1 = load_artifact(FIXTURES / "training_1")
                training_2 = load_artifact(FIXTURES / "training_2")
                heldout = load_artifact(FIXTURES / "heldout")
                expected_playbook = _expected_playbook()
                induction_runner = None if args.live else _fake_induction_runner(expected_playbook)
                playbook = induce_playbook(
                    {"training_1": training_1, "training_2": training_2}, runner=induction_runner
                )
                _say(f"Induced playbook: {playbook.task!r}, {len(playbook.steps)} steps")

                bucket = repository.create_bucket(name="file-expense", target_base_url=base_url)
                repository.add_demonstration(
                    bucket.id, "training_1", training_1["artifact_digest"], "training"
                )
                repository.add_demonstration(
                    bucket.id, "training_2", training_2["artifact_digest"], "training"
                )
                repository.add_demonstration(
                    bucket.id, "heldout", heldout["artifact_digest"], "heldout"
                )
                run_service.activate_capability(
                    bucket.id, reviewed_playbook=playbook.model_dump(mode="json")
                )
                _say("Capability activated at L1 (two training demonstrations, reviewed playbook).")

                # --- 3. Held-out proof: one unique shadow evaluation. ---
                shadow_runner = (
                    None if args.live else _fake_shadow_runner(_fake_shadow_submission())
                )
                shadow_event = record_qualifying_shadow(
                    run_service, bucket_id=bucket.id, playbook_version=1, playbook=playbook,
                    heldout_artifact=heldout, runner=shadow_runner,
                )
                _say(f"Held-out shadow evaluation: {shadow_event.type.value}")

                bucket = confirm_promotion(repository, bucket.id, policy)
                _say(f"Promoted to L{bucket.level} (approval required).")

                rewritten = _rewrite_fixture_host(training_1, base_url)
                fetcher = _authenticated_fetcher(base_url)

                # --- 4. Twin rehearsal + L2 approval + exact production replay. ---
                inputs_1 = _inputs(merchant="Contoso Retail", amount="152.00")
                run_1 = run_service.create_run(bucket.name, inputs_1)
                run_service.start_rehearsal(run_1.id)
                _rehearse_and_store(
                    run_service=run_service, run_id=run_1.id, fixture=rewritten, playbook=playbook,
                    base_url=base_url, fetcher=fetcher, inputs=inputs_1, merchant="Contoso Retail",
                    amount="152.00", live=args.live,
                )
                approval_service.begin_activation(run_1.id)
                _approve_or_wait(approval_service, repository, run_1.id, dashboard_url)
                _, outcome_1 = execute_authorized_run(
                    run_service, run_1.id,
                    file_resolver=lambda _filename, _sha256: _RECEIPT_BYTES,
                    headless=args.headless, authenticate=lambda page: _login(page, base_url),
                )
                _say(
                    f"Production replay succeeded: {outcome_1.succeeded}, "
                    "mutation forwarded exactly once."
                )

                # --- A second approved success promotes to L3. ---
                inputs_2 = _inputs(merchant="Fabrikam Supplies", amount="210.50")
                run_2 = run_service.create_run(bucket.name, inputs_2)
                run_service.start_rehearsal(run_2.id)
                _rehearse_and_store(
                    run_service=run_service, run_id=run_2.id, fixture=rewritten, playbook=playbook,
                    base_url=base_url, fetcher=fetcher, inputs=inputs_2,
                    merchant="Fabrikam Supplies", amount="210.50", live=args.live,
                )
                approval_service.begin_activation(run_2.id)
                _approve_or_wait(approval_service, repository, run_2.id, dashboard_url)
                execute_authorized_run(
                    run_service, run_2.id,
                    file_resolver=lambda _filename, _sha256: _RECEIPT_BYTES,
                    headless=args.headless, authenticate=lambda page: _login(page, base_url),
                )
                bucket = confirm_promotion(repository, bucket.id, policy)
                _say(f"Promoted to L{bucket.level} (durable veto window).")

                # --- L3: a durable veto countdown, then execute once. ---
                inputs_3 = _inputs(merchant="Globex Hardware", amount="64.10")
                run_3 = run_service.create_run(bucket.name, inputs_3)
                run_service.start_rehearsal(run_3.id)
                _rehearse_and_store(
                    run_service=run_service, run_id=run_3.id, fixture=rewritten, playbook=playbook,
                    base_url=base_url, fetcher=fetcher, inputs=inputs_3,
                    merchant="Globex Hardware", amount="64.10", live=args.live,
                )
                approval_service.begin_activation(run_3.id)
                _watch_veto(approval_service, run_3.id, policy.veto_seconds, dashboard_url)
                if repository.get_run(run_3.id).state is RunState.AUTHORIZED:
                    execute_authorized_run(
                        run_service, run_3.id,
                        file_resolver=lambda _filename, _sha256: _RECEIPT_BYTES,
                        headless=args.headless, authenticate=lambda page: _login(page, base_url),
                    )

                mutations = httpx.get(f"{base_url}/api/debug/mutations").json()
                _say(f"Production mutation counter: {mutations['mutation_count']}")

            # portal instance "none" has stopped here; the sidecar/dashboard
            # keeps running underneath the site-change and demotion below.

            # --- 5. A meaningful changed-site failure, at the exact same address. ---
            with _serve(
                build_portal("commit_schema"), portal_port, "demo-portal-changed"
            ) as changed:
                assert changed == base_url
                changed_baseline = httpx.get(f"{changed}/api/debug/mutations").json()[
                    "mutation_count"
                ]
                _say(
                    "Site contract changed underneath the trained capability "
                    "(commit schema drift). The restarted changed-site instance "
                    f"has mutation baseline {changed_baseline}."
                )
                inputs_4 = _inputs(merchant="Initech Office Supply", amount="41.00")
                run_4 = run_service.create_run(bucket.name, inputs_4)
                run_service.start_rehearsal(run_4.id)
                passed = _rehearse_and_store(
                    run_service=run_service, run_id=run_4.id, fixture=rewritten, playbook=playbook,
                    base_url=changed, fetcher=_authenticated_fetcher(changed), inputs=inputs_4,
                    merchant="Initech Office Supply", amount="41.00", live=args.live,
                )
                _say(
                    f"Rehearsal passed: {passed} (expected: False --- "
                    "the drift is caught before any mutation)."
                )
                mutations = httpx.get(f"{changed}/api/debug/mutations").json()
                _say(
                    "Production mutation counter after the failed rehearsal: "
                    f"{mutations['mutation_count']} "
                    f"(unchanged from changed-site baseline {changed_baseline})."
                )

                # --- 6. Demotion. ---
                demoted = repository.get_bucket_by_id(bucket.id)
                _say(
                    f"Capability automatically demoted to L{demoted.level}. "
                    f"See {dashboard_url} for the reason."
                )

                if not args.no_wait:
                    _say(
                        f"Dashboard still open at {dashboard_url}. "
                        "Press Enter to shut everything down."
                    )
                    _wait_for_enter()


if __name__ == "__main__":
    main()
