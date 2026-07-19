from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from agents import Agent
from fastapi import FastAPI, Form, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sse_starlette.sse import EventSourceResponse

from apprentice.config import Policy, load_policy
from apprentice.executor.agent import build_production_agent
from apprentice.executor.sdk_state import SDKStateStore, build_pending_interruption_state
from apprentice.incident import (
    DEFAULT_SCENARIO_ID,
    IncidentNotFoundError,
    IncidentRuntime,
    InvalidIncidentActionError,
)
from apprentice.incident.judgment_graph import JudgmentGraph
from apprentice.incident.progression import (
    IncidentAlreadyAssignedError,
    InvalidReviewSelectionError,
    LearnerNotFoundError,
    LearnerProgression,
)
from apprentice.induction.capability_gap import EXPENSE_POLICY_OUTPUTS, detect_expense_policy_gap
from apprentice.ledger.repository import (
    ConflictError,
    Repository,
    UnknownBucketError,
    UnknownRunError,
)
from apprentice.ledger.transitions import InvalidTransitionError, confirm_promotion
from apprentice.models import (
    Bucket,
    ErrorDetail,
    ErrorResponse,
    Run,
    RunTransition,
)
from apprentice.practice import (
    FinalDebrief,
    InvalidPracticeResponseError,
    PracticeNotReadyForDebriefError,
    PracticeService,
    PracticeSession,
    PracticeSessionNotFoundError,
)
from apprentice.practice.codex_runner import (
    CodexAuthenticationError,
    CodexCLIUnavailableError,
    CodexExecutionError,
    CodexOutputError,
    CodexTimeoutError,
)
from apprentice.selftools.lesson import PolicyExpenseLesson
from apprentice.sidecar import console
from apprentice.sidecar.approval_service import (
    ApprovalService,
    ChecksFailedError,
    NotRehearsedError,
    VetoAlreadyResolvedError,
)
from apprentice.sidecar.run_service import InvalidTargetError, RunService

_ACTIVATION_TOOL_NAME = "activate_rehearsed_plan"
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "trust_policy.yaml"


def _unwired_activation(run_id: str) -> str:
    """The sidecar's default agent exists for SDK state identity, not execution.

    Serializing/deserializing a pending approval interruption only needs the
    agent's tool identity; actually executing an authorized run goes through
    ``apprentice.executor.agent.default_activation`` wired by the deployment,
    never through this stub.
    """
    raise RuntimeError(
        f"Run {run_id}: wire apprentice.executor.agent.default_activation to execute"
    )


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(RequestModel):
    bucket_name: str = Field(min_length=1)
    inputs: dict[str, JsonValue]


class ApprovalDecisionRequest(RequestModel):
    approved: bool
    reason: str | None = None


class IncidentActionRequest(RequestModel):
    kind: str = Field(min_length=1, max_length=40)


class IncidentDelegationRequest(RequestModel):
    actor_id: str = Field(min_length=1, max_length=40)
    request: str = Field(min_length=1, max_length=60)


class CreateIncidentRequest(RequestModel):
    scenario_id: str = Field(default=DEFAULT_SCENARIO_ID, min_length=1, max_length=100)
    seed: int = 0
    learner_id: str | None = Field(default=None, min_length=1, max_length=100)


class CreateLearnerRequest(RequestModel):
    id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=160)


class StartPracticeRequest(RequestModel):
    field: str = Field(min_length=2, max_length=160, pattern=r".*\S.*")
    work_description: str | None = Field(default=None, max_length=2_000)


class PracticeResponseRequest(RequestModel):
    response: str = Field(min_length=1, max_length=8_000, pattern=r".*\S.*")


class AnalyzeTeachingRequest(RequestModel):
    task: str = Field(min_length=3, max_length=500)
    observed_fields: list[str] = Field(min_length=1, max_length=10)


class ActivationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    run: Run
    veto_deadline: float | None = None


def build_sidecar(
    db_path: str | Path | None = None,
    *,
    repository: Repository | None = None,
    run_service: RunService | None = None,
    approval_service: ApprovalService | None = None,
    production_agent: Agent | None = None,
    practice_service: PracticeService | None = None,
    policy: Policy | None = None,
) -> FastAPI:
    if repository is not None and db_path is not None:
        raise ValueError("Pass either repository or db_path, not both")
    active_policy = policy or load_policy(_DEFAULT_POLICY_PATH)
    repository = repository or Repository(db_path or "apprentice.db")
    service = run_service or RunService(repository, policy=active_policy)
    if service.repository is not repository:
        raise ValueError("RunService and app must use the same repository")
    approvals = approval_service or ApprovalService(
        service, veto_seconds=active_policy.veto_seconds, policy=active_policy
    )
    agent = production_agent or build_production_agent(_unwired_activation)
    sdk_state_store = SDKStateStore(agent)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app = FastAPI(title="Apprentice Sidecar")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.state.repository = repository
    app.state.run_service = service
    app.state.approval_service = approvals
    app.state.policy_expense_lesson = PolicyExpenseLesson(repository, service, active_policy)
    app.state.incident_runtime = IncidentRuntime(repository.db)
    app.state.practice_service = practice_service or PracticeService(
        app.state.incident_runtime, database=repository.db
    )
    app.state.learner_progression = LearnerProgression(repository.db)
    app.state.judgment_graph = JudgmentGraph(repository.db, app.state.incident_runtime)

    @app.exception_handler(UnknownBucketError)
    async def unknown_bucket_handler(_request: Request, error: UnknownBucketError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "unknown_bucket", str(error))

    @app.exception_handler(UnknownRunError)
    async def unknown_run_handler(_request: Request, error: UnknownRunError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "unknown_run", str(error))

    @app.exception_handler(InvalidTransitionError)
    async def invalid_transition_handler(
        _request: Request, error: InvalidTransitionError
    ) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "invalid_transition", str(error))

    @app.exception_handler(NotRehearsedError)
    async def not_rehearsed_handler(_request: Request, error: NotRehearsedError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "not_rehearsed", str(error))

    @app.exception_handler(ChecksFailedError)
    async def checks_failed_handler(_request: Request, error: ChecksFailedError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "checks_failed", str(error))

    @app.exception_handler(VetoAlreadyResolvedError)
    async def veto_already_resolved_handler(
        _request: Request, error: VetoAlreadyResolvedError
    ) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "veto_already_resolved", str(error))

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, error: ConflictError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(error))

    @app.exception_handler(InvalidTargetError)
    async def invalid_target_handler(_request: Request, error: InvalidTargetError) -> JSONResponse:
        return _error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_target", str(error))

    @app.exception_handler(IncidentNotFoundError)
    async def incident_not_found_handler(
        _request: Request, error: IncidentNotFoundError
    ) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "unknown_incident", str(error))

    @app.exception_handler(InvalidIncidentActionError)
    async def invalid_incident_action_handler(
        _request: Request, error: InvalidIncidentActionError
    ) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "invalid_incident_action", str(error))

    @app.exception_handler(LearnerNotFoundError)
    async def learner_not_found_handler(
        _request: Request, error: LearnerNotFoundError
    ) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "unknown_learner", str(error))

    @app.exception_handler(IncidentAlreadyAssignedError)
    async def incident_assigned_handler(
        _request: Request, error: IncidentAlreadyAssignedError
    ) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "incident_already_assigned", str(error))

    @app.exception_handler(InvalidReviewSelectionError)
    async def invalid_review_selection_handler(
        _request: Request, error: InvalidReviewSelectionError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_review_selection", str(error)
        )

    @app.exception_handler(PracticeSessionNotFoundError)
    async def practice_not_found_handler(
        request: Request, error: PracticeSessionNotFoundError
    ):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_404_NOT_FOUND,
            code="unknown_practice_session",
            title="This practice could not be found",
            message=str(error.args[0]),
        )

    @app.exception_handler(InvalidPracticeResponseError)
    async def invalid_practice_response_handler(
        request: Request, error: InvalidPracticeResponseError
    ):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_practice_response",
            title="That decision needs another look",
            message=str(error),
        )

    @app.exception_handler(PracticeNotReadyForDebriefError)
    async def practice_not_ready_handler(
        request: Request, error: PracticeNotReadyForDebriefError
    ):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_409_CONFLICT,
            code="practice_not_complete",
            title="The situation is still unfolding",
            message=str(error),
        )

    @app.exception_handler(CodexCLIUnavailableError)
    async def codex_unavailable_handler(request: Request, _error: CodexCLIUnavailableError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="codex_unavailable",
            title="The practice guide is unavailable",
            message="Check that the Codex CLI is installed, then try again.",
        )

    @app.exception_handler(CodexAuthenticationError)
    async def codex_authentication_handler(request: Request, _error: CodexAuthenticationError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="codex_authentication_unavailable",
            title="The practice guide needs authentication",
            message="Sign in with codex login, then return and try again.",
        )

    @app.exception_handler(CodexTimeoutError)
    async def codex_timeout_handler(request: Request, _error: CodexTimeoutError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            code="codex_timeout",
            title="The practice guide took too long",
            message="No practice progress was applied. Return and try this turn again.",
        )

    @app.exception_handler(CodexExecutionError)
    async def codex_execution_handler(request: Request, _error: CodexExecutionError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="codex_execution_failed",
            title="The practice guide could not complete this turn",
            message="Your practice is safe. Return to the situation and try again.",
        )

    @app.exception_handler(CodexOutputError)
    async def codex_output_handler(request: Request, _error: CodexOutputError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="codex_output_invalid",
            title="The practice guide returned an unusable response",
            message="Your practice is safe. Return to the situation and try again.",
        )

    @app.get("/api/buckets", response_model=list[Bucket])
    def list_buckets() -> list[Bucket]:
        return repository.list_buckets()

    @app.post("/api/runs", response_model=Run, status_code=status.HTTP_201_CREATED)
    def create_run(body: CreateRunRequest) -> Run:
        return service.create_run(body.bucket_name, body.inputs)

    @app.get("/api/runs/{run_id}", response_model=Run)
    def get_run(run_id: str) -> Run:
        return repository.get_run(run_id)

    @app.get("/api/runs/{run_id}/transitions", response_model=list[RunTransition])
    def get_run_transitions(run_id: str) -> list[RunTransition]:
        return repository.list_run_transitions(run_id)

    @app.post("/api/runs/{run_id}/activate", response_model=ActivationResponse)
    def activate_run(run_id: str) -> ActivationResponse:
        """Handle one ``activate_rehearsed_plan(run_id)`` approval interruption.

        The sole entry point into the L1-L4 approval boundary: the caller
        supplies only ``run_id`` here too, exactly like the tool itself. The
        sidecar builds and serializes the SDK's own pending-interruption
        ``RunState`` server-side (through the isolated ``SDKStateStore``
        adapter), so an L2/L3 pause persisted here can resume the same SDK
        run state after a restart.
        """
        pending_state = build_pending_interruption_state(
            agent,
            run_id=run_id,
            tool_name=_ACTIVATION_TOOL_NAME,
            call_id=f"call-{run_id}",
        )
        outcome = approvals.begin_activation(
            run_id, sdk_state_text=sdk_state_store.serialize(pending_state)
        )
        return ActivationResponse(
            kind=outcome.kind, run=outcome.run, veto_deadline=outcome.veto_deadline
        )

    @app.post("/api/runs/{run_id}/approval", response_model=Run)
    def resolve_approval(run_id: str, body: ApprovalDecisionRequest) -> Run:
        return approvals.resolve_approval(run_id, approved=body.approved, reason=body.reason)

    @app.post("/api/runs/{run_id}/veto", response_model=Run)
    def cancel_veto(run_id: str) -> Run:
        return approvals.cancel_veto(run_id)

    @app.post("/api/buckets/{bucket_id}/promote", response_model=Bucket)
    def promote_bucket(bucket_id: int) -> Bucket:
        """Apply a promotion the dashboard already showed as eligible.

        Re-verifies eligibility against the live ledger --- the client never
        supplies (and cannot influence) the target level.
        """
        return confirm_promotion(repository, bucket_id, active_policy)

    @app.get("/api/lessons/policy-expense")
    def policy_expense_lesson_status() -> dict[str, object]:
        return app.state.policy_expense_lesson.status()

    @app.post("/api/lessons/policy-expense/teach")
    def teach_policy_expense() -> dict[str, object]:
        return app.state.policy_expense_lesson.teach()

    @app.post("/api/lessons/policy-expense/analyze")
    def analyze_policy_expense_teaching(body: AnalyzeTeachingRequest) -> dict[str, object]:
        task = body.task.strip()
        if "expense" not in task.casefold():
            return {
                "supported": False,
                "assistant_message": (
                    "I observed your demonstration, but this local lesson currently supports "
                    "expense-policy tasks only. Describe an expense task to continue."
                ),
                "missing_capability": None,
            }
        request = detect_expense_policy_gap(
            available_fields=tuple(body.observed_fields),
            required_fields=EXPENSE_POLICY_OUTPUTS,
        )
        if request is None:
            return {
                "supported": False,
                "assistant_message": "I did not detect a supported capability gap from this demo.",
                "missing_capability": None,
            }
        return {
            "supported": True,
            "assistant_message": (
                "I observed merchant, amount, and receipt. This task also needs accounting "
                "category, GL code, cost center, and approval routing. I can draft a read-only "
                "policy lookup tool for your review."
            ),
            "missing_capability": request.name,
        }

    @app.post("/api/lessons/policy-expense/test")
    def test_policy_expense() -> dict[str, object]:
        try:
            return app.state.policy_expense_lesson.test()
        except ValueError as error:
            return _error_response(status.HTTP_409_CONFLICT, "lesson_not_taught", str(error))

    @app.post("/api/incidents", status_code=status.HTTP_201_CREATED)
    def create_incident(body: CreateIncidentRequest | None = None) -> dict[str, object]:
        """Start a fresh, deterministic checkout incident scenario."""
        request = body or CreateIncidentRequest()
        if request.learner_id is not None:
            app.state.learner_progression.profile(request.learner_id)
        snapshot = app.state.incident_runtime.create(
            scenario_id=request.scenario_id, seed=request.seed
        )
        if request.learner_id is not None:
            app.state.learner_progression.assign_run(request.learner_id, str(snapshot["id"]))
        return snapshot

    @app.get("/api/incidents/{incident_id}")
    def get_incident(incident_id: str) -> dict[str, object]:
        return app.state.incident_runtime.snapshot(incident_id)

    @app.post("/api/incidents/{incident_id}/actions")
    def apply_incident_action(incident_id: str, body: IncidentActionRequest) -> dict[str, object]:
        snapshot = app.state.incident_runtime.apply(incident_id, body.kind)
        app.state.learner_progression.record_outcome(snapshot)
        return snapshot

    @app.get("/api/incidents/{incident_id}/actors/{actor_id}")
    def incident_actor_view(incident_id: str, actor_id: str) -> dict[str, object]:
        return app.state.incident_runtime.actor_view(incident_id, actor_id)

    @app.post("/api/incidents/{incident_id}/delegations")
    def delegate_incident_work(
        incident_id: str, body: IncidentDelegationRequest
    ) -> dict[str, object]:
        snapshot = app.state.incident_runtime.delegate(
            incident_id, body.actor_id, body.request
        )
        app.state.learner_progression.record_outcome(snapshot)
        return snapshot

    @app.get("/api/incidents/{incident_id}/replay")
    def incident_replay(
        incident_id: str, at: int | None = Query(default=None, ge=0)
    ) -> dict[str, object]:
        """List causal events or return the exact canonical state at ``at``.

        ``at`` is the zero-based event index returned in each replay event.
        Omitting it retains the compact event-list response for timeline UIs.
        """
        if at is not None:
            return {
                "incident_id": incident_id,
                "event_index": at,
                "snapshot": app.state.incident_runtime.replay_snapshot(incident_id, at),
            }
        return {
            "incident_id": incident_id,
            "events": app.state.incident_runtime.replay(incident_id),
        }

    @app.get("/api/incidents/{incident_id}/debrief")
    def incident_debrief(incident_id: str) -> dict[str, object]:
        snapshot = app.state.incident_runtime.snapshot(incident_id)
        return {
            "incident_id": incident_id,
            "completed": snapshot["completed"],
            "outcome": snapshot["outcome"],
            "debrief": snapshot["debrief"],
        }

    @app.get("/api/incidents/{incident_id}/judgment-graph")
    def incident_judgment_graph(incident_id: str) -> dict[str, object]:
        """Reconstruct the run's inspectable judgment evidence graph."""
        return app.state.judgment_graph.incident(incident_id)

    @app.post("/api/learners", status_code=status.HTTP_201_CREATED)
    def create_learner(body: CreateLearnerRequest) -> dict[str, object]:
        return app.state.learner_progression.create_learner(body.id, body.display_name)

    @app.get("/api/learners/{learner_id}")
    def learner_progression(learner_id: str) -> dict[str, object]:
        return app.state.learner_progression.profile(learner_id)

    @app.get("/api/learners/{learner_id}/history")
    def learner_history(learner_id: str) -> dict[str, object]:
        profile = app.state.learner_progression.profile(learner_id)
        return {
            "learner_id": profile["id"],
            "completed_runs": profile["completed_runs"],
            "history": profile["history"],
        }

    @app.get("/api/learners/{learner_id}/judgment-graph")
    def learner_judgment_graph(learner_id: str) -> dict[str, object]:
        """Aggregate completed-run judgment signals without opaque scoring."""
        return app.state.judgment_graph.learner(learner_id)

    @app.get("/api/instructor/review")
    def instructor_review(
        learner_id: Annotated[list[str], Query(min_length=1)],
    ) -> dict[str, object]:
        """Compare explicitly selected learner profiles for an instructor review.

        The query parameter is repeatable, for example
        ``?learner_id=ada&learner_id=grace``.  This avoids silently creating a
        mutable team roster while still providing a durable, evidence-backed
        view over completed simulation attempts.
        """
        return app.state.learner_progression.compare(learner_id)

    @app.post("/api/practice/sessions", status_code=status.HTTP_201_CREATED)
    def create_practice_session(body: StartPracticeRequest) -> dict[str, object]:
        session = app.state.practice_service.start(
            body.field.strip(), _optional_text(body.work_description)
        )
        return session.model_dump(mode="json")

    @app.get("/api/practice/sessions/{session_id}")
    def get_practice_session(session_id: str) -> dict[str, object]:
        return app.state.practice_service.get_session(session_id).model_dump(mode="json")

    @app.post("/api/practice/sessions/{session_id}/responses")
    def submit_practice_response(
        session_id: str, body: PracticeResponseRequest
    ) -> dict[str, object]:
        session = app.state.practice_service.respond(session_id, body.response.strip())
        return session.model_dump(mode="json")

    @app.get("/api/practice/sessions/{session_id}/debrief")
    def get_practice_debrief(session_id: str) -> dict[str, object]:
        return app.state.practice_service.get_debrief(session_id).model_dump(mode="json")

    @app.get("/")
    def home(request: Request):
        return templates.TemplateResponse(request, "dojo_entry.html", {})

    @app.post("/practice")
    def start_practice(
        field: Annotated[str, Form(min_length=2, max_length=160, pattern=r".*\S.*")],
        work_description: Annotated[str | None, Form(max_length=2_000)] = None,
    ) -> RedirectResponse:
        session = app.state.practice_service.start(
            field.strip(), _optional_text(work_description)
        )
        return RedirectResponse(
            url=f"/practice/{session.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/practice/{session_id}")
    def practice_page(request: Request, session_id: str):
        session = app.state.practice_service.get_session(session_id)
        return templates.TemplateResponse(
            request, "dojo_practice.html", {"session": _practice_page_context(session)}
        )

    @app.post("/practice/{session_id}/responses")
    def practice_response(
        session_id: str,
        response: Annotated[str, Form(min_length=1, max_length=8_000, pattern=r".*\S.*")],
    ) -> RedirectResponse:
        session = app.state.practice_service.respond(session_id, response.strip())
        destination = (
            f"/practice/{session_id}/debrief"
            if session.world.get("completed") or session.world.get("terminal")
            else f"/practice/{session_id}"
        )
        return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/practice/{session_id}/debrief")
    def practice_debrief_page(request: Request, session_id: str):
        session = app.state.practice_service.get_session(session_id)
        debrief = app.state.practice_service.get_debrief(session_id)
        return templates.TemplateResponse(
            request,
            "dojo_debrief.html",
            {
                "session": _practice_page_context(session),
                "debrief": _debrief_page_context(debrief),
            },
        )

    @app.get("/legacy")
    def legacy_dashboard_page(request: Request):
        context = console.build_dashboard_context(repository, active_policy)
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/console/dashboard")
    def dashboard_page(request: Request):
        context = console.build_dashboard_context(repository, active_policy)
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/console/runs/{run_id}")
    def run_page(request: Request, run_id: str):
        context = console.build_run_context(repository, run_id)
        return templates.TemplateResponse(request, "run.html", context)

    @app.get("/console/capabilities/{bucket_id}")
    def capability_page(request: Request, bucket_id: int):
        context = console.build_capability_context(repository, active_policy, bucket_id)
        return templates.TemplateResponse(request, "capability.html", context)

    @app.get("/console/dashboard/stream")
    async def dashboard_stream(request: Request) -> EventSourceResponse:
        async def event_source():
            async for context in console.dashboard_events(repository, active_policy):
                if await request.is_disconnected():
                    break
                yield {
                    "event": console.DASHBOARD_SSE_EVENT,
                    "data": json.dumps(context, default=str),
                }

        return EventSourceResponse(event_source())

    return app


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _practice_error_response(
    request: Request,
    templates: Jinja2Templates,
    *,
    status_code: int,
    code: str,
    title: str,
    message: str,
):
    if request.url.path.startswith("/api/"):
        return _error_response(status_code, code, message)
    session_id = (
        None
        if code == "unknown_practice_session"
        else _practice_session_id_from_path(request.url.path)
    )
    return templates.TemplateResponse(
        request,
        "dojo_error.html",
        {
            "status": status_code,
            "title": title,
            "message": message,
            "return_href": f"/practice/{session_id}" if session_id else "/",
            "return_label": "Return to the situation" if session_id else "Enter the dojo",
        },
        status_code=status_code,
    )


def _practice_session_id_from_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "practice":
        return parts[1]
    return None


create_app = build_sidecar


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _practice_page_context(session: PracticeSession) -> dict[str, object]:
    raw = session.model_dump(mode="json")
    scenario = raw["scenario"]
    world = raw["world"]
    turns = raw["turns"]
    developments = scenario["how_it_developed"]
    timeline = [
        item
        if isinstance(item, dict)
        else {"time": f"{index + 1:02}", "label": str(item)}
        for index, item in enumerate(developments)
    ]
    events = world.get("events", [])
    latest_event = events[-1] if events else None
    if isinstance(latest_event, dict):
        situational_update = latest_event.get("detail") or latest_event.get("summary")
    else:
        situational_update = latest_event
    if turns:
        situational_update = turns[-1].get("coaching_question") or situational_update
    presented_turns = [_practice_turn_context(turn) for turn in turns]
    return {
        "id": raw["id"],
        "stage": "decision" if turns else "briefing",
        "title": scenario["title"],
        "summary": scenario["briefing"],
        "timeline": timeline,
        "role": scenario["learner_role"],
        "constraint": " · ".join(scenario["immediate_constraints"]),
        "first_decision": scenario["first_decision"],
        "situational_update": situational_update,
        "latest_turn": presented_turns[-1] if presented_turns else None,
        "prior_turns": presented_turns[:-1],
    }


def _practice_turn_context(turn: dict[str, object]) -> dict[str, object]:
    assessment = turn["assessment"]
    assert isinstance(assessment, dict)
    return {
        "response": turn["response"],
        "response_excerpt": assessment.get("response_excerpt"),
        "interpretation": assessment["interpretation"],
        "strength": assessment["strength"],
        "risk": assessment["risk"],
        "evidence": assessment["evidence"],
        "coaching_question": turn.get("coaching_question"),
        "action": turn.get("action_kind"),
        "outcome": turn["outcome"],
    }


def _debrief_page_context(debrief: FinalDebrief) -> dict[str, object]:
    raw = debrief.model_dump(mode="json")
    return {
        "what_you_saw": raw["noticed"],
        "what_you_missed": raw["missed"],
        "strong_decisions": raw["strong_decisions"],
        "risky_assumptions": raw["risky_assumptions"],
        "stronger_path": raw["better_path"],
        "expert_approach": raw["expert_approach"],
        "carry_forward": raw["carry_forward"],
        "evidence": raw["evidence"],
    }
