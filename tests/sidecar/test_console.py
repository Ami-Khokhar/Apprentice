from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from starlette.templating import Jinja2Templates

from apprentice.canonical import digest
from apprentice.config import load_policy
from apprentice.ledger.repository import Repository
from apprentice.models import RecordedAction
from apprentice.sidecar import console
from apprentice.sidecar.app import build_sidecar
from apprentice.sidecar.approval_service import ApprovalService
from apprentice.sidecar.run_service import RunService
from apprentice.twin.invariants import CHECK_NAMES

ROOT = Path(__file__).parents[2]
POLICY = load_policy(ROOT / "trust_policy.yaml")
TEMPLATES_DIR = ROOT / "apprentice" / "sidecar" / "templates"
TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))

_ALL_CHECKS_PASS = dict.fromkeys(CHECK_NAMES, True)


def _activated_bucket(
    repository: Repository, *, risk_class: str = "medium", name: str = "file-expense"
):
    bucket = repository.create_bucket(name=name, risk_class=risk_class)
    repository.add_demonstration(bucket.id, "training-1", f"{name}-training-1", "training")
    repository.add_demonstration(bucket.id, "training-2", f"{name}-training-2", "training")
    repository.add_demonstration(bucket.id, "heldout-1", f"{name}-heldout-1", "heldout")
    service = RunService(repository, policy=POLICY)
    activated = service.activate_capability(bucket.id, reviewed_playbook={"task": "file expense"})
    return service, activated


def _set_level(repository: Repository, bucket_id: int, level: int) -> None:
    with repository.db.transaction(write=True) as connection:
        connection.execute("UPDATE buckets SET level = ? WHERE id = ?", (level, bucket_id))


def test_dashboard_context_labels_the_active_policy_demo_policy(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    _activated_bucket(repository)

    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    assert context["policy_label"] == "Demo Policy"


def test_dashboard_context_includes_capability_card_fields(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository, risk_class="high")
    service.record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=bucket.playbook_version,
        heldout_artifact_digest=f"{bucket.name}-heldout-1",
        passed=True,
    )

    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    assert len(context["capabilities"]) == 1
    card = context["capabilities"][0]
    assert card["bucket_id"] == bucket.id
    assert card["name"] == "file-expense"
    assert card["task"] == "file expense"
    assert card["risk_class"] == "high"
    assert card["level"] == 1
    assert card["effective_level"] == 1
    assert len(card["ladder"]) == 5
    assert card["ladder"][0]["level"] == 0
    assert card["ladder"][1]["reached"] is True
    assert card["ladder"][4]["reached"] is False
    assert card["unique_evidence"] == {"shadow_pass": 1}
    assert 0.0 <= card["score"] <= 1.0
    # High risk requires 2 unique shadow passes for L2; only 1 recorded.
    assert card["next_promotion"]["eligible"] is False
    assert any("unique shadow passes 1/2" in reason for reason in card["next_promotion"]["unmet"])
    assert card["latest_demotion_cause"] is None
    assert card["path"] == f"/console/capabilities/{bucket.id}"
    assert card["promotion_reasons"]


def test_capability_context_includes_runs_and_plain_promotion_guidance(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository)
    run = service.create_run(bucket.name, {"amount": "42.00"})

    context = console.build_capability_context(repository, POLICY, bucket.id, now=1_000.0)

    assert context["capability"]["bucket_id"] == bucket.id
    assert context["recent_runs"][0]["id"] == run.id
    assert context["recent_runs"][0]["bucket_id"] == bucket.id
    assert "held-out shadow evaluation" in context["promotion_reasons"][0]
    assert context["overview"]["total_capabilities"] == 1
    assert context["overview"]["activated_capabilities"] == 1


def test_dashboard_context_shows_promote_only_when_eligible(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository, risk_class="low", name="eligible-bucket")
    service.record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=bucket.playbook_version,
        heldout_artifact_digest=f"{bucket.name}-heldout-1",
        passed=True,
    )

    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    card = context["capabilities"][0]
    assert card["next_promotion"]["eligible"] is True
    assert card["next_promotion"]["target_level"] == 2


def test_dashboard_context_reports_latest_demotion_cause(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository)
    _set_level(repository, bucket.id, 3)
    run = service.create_run(bucket.name, {"amount": "1.00"})
    service.start_rehearsal(run.id)

    service.record_rehearsal_result(run.id, passed=False)

    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)
    card = context["capabilities"][0]
    assert card["level"] == 2
    assert card["latest_demotion_cause"] is not None
    assert "major" in card["latest_demotion_cause"]


def test_dashboard_context_applies_lazy_idle_decay(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    _service, bucket = _activated_bucket(repository)
    _set_level(repository, bucket.id, 3)
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE buckets SET last_activity_at = 0 WHERE id = ?", (bucket.id,)
        )
    interval = POLICY.decay_idle_days * 86_400

    context = console.build_dashboard_context(repository, POLICY, now=interval)

    card = context["capabilities"][0]
    assert card["level"] == 2
    assert repository.get_bucket_by_id(bucket.id).level == 2


def test_effective_level_reflects_declared_tool_dependencies(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    tool_bucket = repository.create_bucket(name="browser-tool", kind="tool")
    _set_level(repository, tool_bucket.id, 2)
    _service, bucket = _activated_bucket(repository, name="file-expense")
    with repository.db.transaction(write=True) as connection:
        connection.execute(
            "UPDATE buckets SET level = 4, tool_versions_json = ? WHERE id = ?",
            ('{"browser-tool": "1.0"}', bucket.id),
        )

    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    card = next(c for c in context["capabilities"] if c["name"] == "file-expense")
    assert card["level"] == 4
    assert card["effective_level"] == 2


def _rehearsed_run(repository: Repository, service: RunService, *, level: int):
    _set_level(repository, repository.get_bucket_by_name("file-expense").id, level)
    run = service.create_run("file-expense", {"amount": "1.00"})
    service.start_rehearsal(run.id)
    navigate_arguments = {"path": "/expense"}
    commit_arguments = {"payload_digest": "test", "method": "POST", "url": "http://127.0.0.1/expense"}
    actions = [
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
    service.record_rehearsal_result(
        run.id, passed=True, checks=_ALL_CHECKS_PASS, trace_ref="trace-1", actions=actions
    )
    return run


def test_run_context_includes_all_six_named_invariants(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=2)

    context = console.build_run_context(repository, run.id)

    assert [item["name"] for item in context["checklist"]] == list(CHECK_NAMES)
    assert all(item["status"] is True for item in context["checklist"])


def test_run_context_reflects_approval_pending_state_and_approval_urls(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=2)
    approvals = ApprovalService(service, veto_seconds=5.0, clock=lambda: 1_000.0)
    approvals.begin_activation(run.id)

    context = console.build_run_context(repository, run.id)

    assert context["state"] == "approval_pending"
    assert context["approval_urls"]["activate"] == f"/api/runs/{run.id}/activate"
    assert context["approval_urls"]["approve"] == f"/api/runs/{run.id}/approval"
    assert context["approval_urls"]["reject"] == f"/api/runs/{run.id}/approval"
    assert context["approval_urls"]["cancel_veto"] == f"/api/runs/{run.id}/veto"


def test_run_context_reflects_veto_state(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=3)
    approvals = ApprovalService(service, veto_seconds=5.0, clock=lambda: 1_000.0)
    approvals.begin_activation(run.id)

    context = console.build_run_context(repository, run.id)

    assert context["state"] == "veto_pending"
    assert context["veto"]["status"] == "pending"
    assert context["veto"]["deadline"] == 1_005.0


def test_run_context_reports_production_replay_and_verified_outcome(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=4)
    approvals = ApprovalService(service, veto_seconds=5.0, clock=lambda: 1_000.0)
    approvals.begin_activation(run.id)
    service.start_execution(run.id)
    service.record_verified_outcome(run.id, succeeded=True)

    context = console.build_run_context(repository, run.id)

    assert context["state"] == "succeeded"
    assert context["production_replay"]["executed"] is True
    assert context["outcome"]["succeeded"] is True


def test_run_context_defaults_when_no_run_exists(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")

    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    assert context["run_id"] is None
    assert [item["name"] for item in context["checklist"]] == list(CHECK_NAMES)
    assert all(item["status"] is None for item in context["checklist"])


def test_dashboard_template_renders_policy_label_and_capability(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    _activated_bucket(repository)
    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    html = TEMPLATES.get_template("dashboard.html").render(**context)

    assert "Demo Policy" in html
    assert "file-expense" in html
    for level in range(5):
        assert f"L{level}" in html
    assert "Create a run" in html
    assert f"/console/capabilities/{repository.get_bucket_by_name('file-expense').id}" in html


def test_run_template_displays_all_six_named_invariants(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=2)
    context = console.build_run_context(repository, run.id)

    html = TEMPLATES.get_template("run.html").render(**context)

    for name in CHECK_NAMES:
        assert name in html


def test_run_template_offers_activate_button_for_rehearsed_run(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=2)
    context = console.build_run_context(repository, run.id)

    html = TEMPLATES.get_template("run.html").render(**context)

    assert f'action="/api/runs/{run.id}/activate"' in html
    assert "Activate rehearsed plan" in html


def test_approval_buttons_call_only_the_approval_service_endpoints(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=2)
    approvals = ApprovalService(service, veto_seconds=5.0, clock=lambda: 1_000.0)
    approvals.begin_activation(run.id)
    context = console.build_run_context(repository, run.id)

    html = TEMPLATES.get_template("run.html").render(**context)

    assert f'action="/api/runs/{run.id}/approval"' in html
    assert f'action="/api/runs/{run.id}/veto"' not in html


def test_promote_control_shown_only_when_eligible(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, eligible_bucket = _activated_bucket(repository, risk_class="low", name="eligible")
    service.record_shadow_result(
        bucket_id=eligible_bucket.id,
        playbook_version=eligible_bucket.playbook_version,
        heldout_artifact_digest=f"{eligible_bucket.name}-heldout-1",
        passed=True,
    )
    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    html = TEMPLATES.get_template("dashboard.html").render(**context)

    assert f'action="/api/buckets/{eligible_bucket.id}/promote"' in html


def test_promote_control_absent_when_not_eligible(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    _service, bucket = _activated_bucket(repository, name="not-eligible")
    context = console.build_dashboard_context(repository, POLICY, now=1_000.0)

    html = TEMPLATES.get_template("dashboard.html").render(**context)

    assert f'action="/api/buckets/{bucket.id}/promote"' not in html


def test_sse_event_name_wiring_matches_between_server_and_template() -> None:
    """The stream route emits a NAMED SSE event; `onmessage` would never fire for it.

    Regression guard for the event-name mismatch: the template must subscribe
    with addEventListener using exactly the name the server emits (both sides
    reference ``console.DASHBOARD_SSE_EVENT``), and must not rely on the
    default unnamed-``message`` handler.
    """
    template_source = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert f'addEventListener("{console.DASHBOARD_SSE_EVENT}"' in template_source
    assert "source.onmessage" not in template_source

    app_source = (ROOT / "apprentice" / "sidecar" / "app.py").read_text(encoding="utf-8")
    assert 'console.DASHBOARD_SSE_EVENT' in app_source
    assert '"event": "dashboard"' not in app_source


def test_latest_demotion_cause_prefers_the_most_recent_of_failure_and_idle_decay(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository)
    _set_level(repository, bucket.id, 3)
    run = service.create_run(bucket.name, {"amount": "1.00"})
    service.start_rehearsal(run.id)
    service.record_rehearsal_result(run.id, passed=False)  # major demotion, now ~wall clock
    interval = POLICY.decay_idle_days * 86_400
    failure_time = repository.list_events(bucket.id)[-1].created_at

    # An idle decay lands AFTER the major failure: it is the latest cause.
    context = console.build_dashboard_context(repository, POLICY, now=failure_time + interval)

    card = context["capabilities"][0]
    assert "idle decay" in card["latest_demotion_cause"]


async def test_dashboard_events_yields_dashboard_context(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    _activated_bucket(repository)

    generator = console.dashboard_events(
        repository, POLICY, interval_seconds=0.0, clock=lambda: 1_000.0
    )
    first = await anext(generator)
    await generator.aclose()

    assert first["policy_label"] == "Demo Policy"
    assert len(first["capabilities"]) == 1


def test_http_console_routes_render_dashboard_and_run_pages(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, _bucket = _activated_bucket(repository)
    run = _rehearsed_run(repository, service, level=2)
    client = TestClient(build_sidecar(repository=repository, run_service=service, policy=POLICY))

    dashboard_response = client.get("/console/dashboard")
    run_response = client.get(f"/console/runs/{run.id}")

    assert dashboard_response.status_code == 200
    assert "Apprentice control console" in dashboard_response.text
    assert run_response.status_code == 200
    for name in CHECK_NAMES:
        assert name in run_response.text


def test_http_root_renders_dojo_entry(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    client = TestClient(build_sidecar(repository=repository, policy=POLICY))

    response = client.get("/")

    assert response.status_code == 200
    assert "What work do you want to master?" in response.text


def test_http_legacy_renders_original_dashboard(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    client = TestClient(build_sidecar(repository=repository, policy=POLICY))

    response = client.get("/legacy")

    assert response.status_code == 200
    assert "Apprentice control console" in response.text


def test_http_promote_endpoint_applies_a_confirmed_promotion(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository, risk_class="low")
    service.record_shadow_result(
        bucket_id=bucket.id,
        playbook_version=bucket.playbook_version,
        heldout_artifact_digest=f"{bucket.name}-heldout-1",
        passed=True,
    )
    client = TestClient(build_sidecar(repository=repository, run_service=service, policy=POLICY))

    response = client.post(f"/api/buckets/{bucket.id}/promote")

    assert response.status_code == 200
    assert response.json()["level"] == 2


def test_http_promote_endpoint_rejects_when_not_eligible(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    service, bucket = _activated_bucket(repository)
    client = TestClient(build_sidecar(repository=repository, run_service=service, policy=POLICY))

    response = client.post(f"/api/buckets/{bucket.id}/promote")

    assert response.status_code == 409
