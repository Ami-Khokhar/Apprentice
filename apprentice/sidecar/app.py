from __future__ import annotations

import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from apprentice.database import CURRENT_OWNER_ID, SQLiteDatabase
from apprentice.observability import LocalTraceStore, build_tracer, enabled
from apprentice.practice import (
    FinalDebrief,
    InvalidPracticeResponseError,
    PracticeBusyError,
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
from apprentice.practice.openai_runner import CURRENT_API_KEY, OpenAIStructuredRunner

VISITOR_COOKIE = "apprentice_visitor"
API_KEY_COOKIE = "apprentice_api_key"
_VISITOR_ID = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")
_API_KEY = re.compile(r"\A[A-Za-z0-9_.\-]{20,200}\Z")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartPracticeRequest(RequestModel):
    field: str = Field(min_length=2, max_length=160, pattern=r".*\S.*")
    work_description: str | None = Field(default=None, max_length=2_000)
    difficulty_level: int = Field(default=1, ge=1, le=10)


class PracticeResponseRequest(RequestModel):
    response: str = Field(min_length=1, max_length=8_000, pattern=r".*\S.*")


class ClarificationRequest(RequestModel):
    question: str = Field(min_length=1, max_length=2_000, pattern=r".*\S.*")


class JudgmentProfileRequest(RequestModel):
    display_name: str = Field(min_length=1, max_length=120, pattern=r".*\S.*")
    headline: str = Field(default="", max_length=240)
    bio: str = Field(default="", max_length=2_000)


class ObserverSessionRequest(RequestModel):
    token: str = Field(min_length=1, max_length=1_024)


def build_app(
    db_path: str | Path | None = None,
    *,
    database: SQLiteDatabase | None = None,
    practice_service: PracticeService | None = None,
    trace_store: LocalTraceStore | None = None,
    environ: dict[str, str] | None = None,
) -> FastAPI:
    if database is not None and db_path is not None:
        raise ValueError("Pass either database or db_path, not both")

    environment = os.environ if environ is None else environ
    observer_enabled = enabled(environment.get("APPRENTICE_OBSERVER_ENABLED"))
    multi_user = enabled(environment.get("APPRENTICE_MULTI_USER"))
    public_host = environment.get("APPRENTICE_PUBLIC_HOST", "").strip().lower()
    observer_token = environment.get("APPRENTICE_OBSERVER_TOKEN", "")
    if observer_enabled and not observer_token:
        raise ValueError(
            "APPRENTICE_OBSERVER_TOKEN is required when APPRENTICE_OBSERVER_ENABLED=true"
        )
    database = database or SQLiteDatabase(
        db_path or environment.get("APPRENTICE_DB_PATH") or "apprentice.db"
    )
    if observer_enabled and trace_store is None:
        trace_store = LocalTraceStore(
            environment.get("APPRENTICE_TRACE_PATH", ".apprentice/traces.jsonl"),
            retention_days=_environment_int(environment.get("APPRENTICE_TRACE_RETENTION_DAYS"), 30),
            content_limit_bytes=_environment_int(
                environment.get("APPRENTICE_TRACE_CONTENT_LIMIT_BYTES"), 256_000
            ),
        )
    practice_model = environment.get("APPRENTICE_PRACTICE_MODEL", "").strip()
    if multi_user and not practice_model:
        raise ValueError("APPRENTICE_PRACTICE_MODEL is required when APPRENTICE_MULTI_USER=true")
    service = practice_service or PracticeService(
        database=database,
        tracer=build_tracer(environment, local_store=trace_store),
        **({"runner": OpenAIStructuredRunner(), "model": practice_model} if multi_user else {}),
    )
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app = FastAPI(title="Apprentice")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.state.database = database
    app.state.practice_service = service
    app.state.trace_store = trace_store

    allowed_hosts = {
        "127.0.0.1",
        "::1",
        "localhost",
        "testserver",
    }
    if multi_user and public_host:
        allowed_hosts.add(public_host)
    # A hosted deployment answers on its platform hostname. Enforce the allowlist there
    # only when the operator has named that host.
    enforce_hosts = not multi_user or bool(public_host)

    @app.middleware("http")
    async def enforce_local_request_boundary(request: Request, call_next):
        if enforce_hosts and _request_hostname(request) not in allowed_hosts:
            return _request_boundary_response(request, status.HTTP_400_BAD_REQUEST)
        if not multi_user and not _is_loopback(request):
            return _request_boundary_response(request, status.HTTP_403_FORBIDDEN)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _same_origin(request):
            return _error_response(
                status.HTTP_403_FORBIDDEN,
                "cross_origin_request_rejected",
                "Cross-origin state changes are not allowed.",
            )
        return await call_next(request)

    if multi_user:

        @app.middleware("http")
        async def assign_visitor(request: Request, call_next):
            """Give each browser its own owner id, so practice stays private to it."""
            cookie = request.cookies.get(VISITOR_COOKIE, "")
            visitor = cookie if _VISITOR_ID.fullmatch(cookie) else secrets.token_urlsafe(24)
            supplied = request.cookies.get(API_KEY_COOKIE, "")
            owner_token = CURRENT_OWNER_ID.set(visitor)
            key_token = CURRENT_API_KEY.set(supplied if _API_KEY.fullmatch(supplied) else "")
            try:
                response = await call_next(request)
            finally:
                CURRENT_API_KEY.reset(key_token)
                CURRENT_OWNER_ID.reset(owner_token)
            if visitor != cookie:
                response.set_cookie(
                    VISITOR_COOKIE,
                    visitor,
                    max_age=60 * 60 * 24 * 30,
                    httponly=True,
                    samesite="lax",
                    secure=request.url.scheme == "https",
                )
            return response

    @app.exception_handler(PracticeSessionNotFoundError)
    async def practice_not_found_handler(request: Request, error: PracticeSessionNotFoundError):
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
    async def practice_not_ready_handler(request: Request, error: PracticeNotReadyForDebriefError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_409_CONFLICT,
            code="practice_not_complete",
            title="The situation is still unfolding",
            message=str(error),
        )

    @app.exception_handler(PracticeBusyError)
    async def practice_busy_handler(request: Request, _error: PracticeBusyError):
        response = _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="practice_busy",
            title="The practice guide is busy",
            message="Another practice turn is still being prepared. Try again shortly.",
        )
        response.headers["Retry-After"] = "1"
        return response

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
    async def codex_authentication_handler(request: Request, error: CodexAuthenticationError):
        return _practice_error_response(
            request,
            templates,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="codex_authentication_unavailable",
            title="The practice guide needs authentication",
            message=(
                str(error.args[0])
                if multi_user and error.args
                else "Sign in with codex login, then return and try again."
            ),
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

    @app.post("/api/practice/sessions", status_code=status.HTTP_201_CREATED)
    def create_practice_session(body: StartPracticeRequest) -> dict[str, object]:
        session = service.start(
            body.field.strip(), _optional_text(body.work_description), body.difficulty_level
        )
        return session.model_dump(mode="json")

    @app.get("/api/practice/sessions/{session_id}")
    def get_practice_session(session_id: str) -> dict[str, object]:
        return service.get_session(session_id).model_dump(mode="json")

    @app.post("/api/practice/sessions/{session_id}/responses")
    def submit_practice_response(
        session_id: str, body: PracticeResponseRequest
    ) -> dict[str, object]:
        session = service.respond(session_id, body.response.strip())
        return session.model_dump(mode="json")

    @app.post("/api/practice/sessions/{session_id}/clarifications")
    def ask_practice_clarification(
        session_id: str, body: ClarificationRequest
    ) -> dict[str, object]:
        session = service.clarify(session_id, body.question.strip())
        return session.model_dump(mode="json")

    @app.post("/api/practice/sessions/{session_id}/stop")
    def stop_practice_session(session_id: str) -> dict[str, object]:
        session = service.stop(session_id)
        return session.model_dump(mode="json")

    @app.get("/api/practice/sessions/{session_id}/debrief")
    def get_practice_debrief(session_id: str) -> dict[str, object]:
        return service.get_debrief(session_id).model_dump(mode="json")

    @app.get("/api/profile")
    def get_judgment_profile() -> dict[str, object]:
        profile = service.get_judgment_profile()
        return {
            "profile": profile.model_dump(mode="json"),
            "cases": [item.model_dump(mode="json") for item in service.list_portfolio_cases()],
        }

    @app.put("/api/profile")
    def update_judgment_profile(body: JudgmentProfileRequest) -> dict[str, object]:
        return service.update_judgment_profile(
            body.display_name, body.headline, body.bio
        ).model_dump(mode="json")

    @app.get("/api/profile/cases/{session_id}")
    def get_portfolio_case(session_id: str) -> dict[str, object]:
        return service.get_portfolio_case(session_id).model_dump(mode="json")

    @app.get("/")
    def home(request: Request):
        return templates.TemplateResponse(
            request,
            "dojo_entry.html",
            {
                "hosted": multi_user,
                "has_key": bool(CURRENT_API_KEY.get()),
                "key_rejected": request.query_params.get("key") == "rejected",
            },
        )

    if multi_user:

        @app.post("/key")
        def remember_api_key(
            request: Request,
            api_key: Annotated[str, Form(max_length=200)] = "",
        ) -> RedirectResponse:
            """Hold the learner's key in a session cookie. It is never written to disk."""
            candidate = api_key.strip()
            if not _API_KEY.fullmatch(candidate):
                return RedirectResponse(url="/?key=rejected", status_code=status.HTTP_303_SEE_OTHER)
            response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(
                API_KEY_COOKIE,
                candidate,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
            return response

        @app.post("/key/forget")
        def forget_api_key() -> RedirectResponse:
            response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie(API_KEY_COOKIE)
            return response

    @app.get("/profile", response_class=HTMLResponse)
    def judgment_profile_page(request: Request):
        profile = service.get_judgment_profile()
        cases = service.list_portfolio_cases()
        return templates.TemplateResponse(
            request,
            "dojo_profile.html",
            _profile_page_context(profile.model_dump(mode="json"), cases),
        )

    @app.post("/profile")
    def update_judgment_profile_page(
        display_name: Annotated[str, Form(min_length=1, max_length=120, pattern=r".*\S.*")],
        headline: Annotated[str, Form(max_length=240)] = "",
        bio: Annotated[str, Form(max_length=2_000)] = "",
    ) -> RedirectResponse:
        service.update_judgment_profile(display_name, headline, bio)
        return RedirectResponse(url="/profile", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/profile/cases/{session_id}", response_class=HTMLResponse)
    def portfolio_case_page(request: Request, session_id: str):
        case = service.get_portfolio_case(session_id)
        return templates.TemplateResponse(
            request,
            "dojo_case.html",
            {"case": _portfolio_case_context(case.model_dump(mode="json"))},
        )

    if observer_enabled:

        def observer_allowed(request: Request) -> bool:
            if not _is_loopback(request):
                return False
            authorization = request.headers.get("authorization", "")
            bearer = authorization[7:] if authorization.casefold().startswith("bearer ") else ""
            supplied = bearer or request.cookies.get("apprentice_observer", "")
            return bool(supplied) and hmac.compare_digest(supplied, observer_token)

        @app.get("/observer", response_class=HTMLResponse)
        def observer_dashboard(request: Request):
            if not _is_loopback(request):
                return _observer_not_found()
            if not observer_allowed(request):
                return templates.TemplateResponse(request, "observer_login.html", {})
            return templates.TemplateResponse(
                request,
                "observer.html",
                {
                    "content_capture": enabled(environment.get("APPRENTICE_TRACE_CONTENT")),
                    "retention_days": _environment_int(
                        environment.get("APPRENTICE_TRACE_RETENTION_DAYS"), 30
                    ),
                    "langfuse_enabled": enabled(environment.get("APPRENTICE_LANGFUSE_ENABLED")),
                    "langfuse_base_url": environment.get("LANGFUSE_BASE_URL", ""),
                },
            )

        @app.post("/api/observer/session")
        def create_observer_session(request: Request, body: ObserverSessionRequest):
            if not _is_loopback(request) or not hmac.compare_digest(body.token, observer_token):
                return _observer_not_found()
            response = JSONResponse({"authenticated": True})
            response.set_cookie(
                "apprentice_observer",
                observer_token,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response

        @app.get("/api/observer/traces")
        def observer_traces(request: Request):
            if not observer_allowed(request):
                return _observer_not_found()
            assert trace_store is not None
            return {"traces": trace_store.sessions()}

        @app.get("/api/observer/traces/{session_id}")
        def observer_trace(request: Request, session_id: str):
            if not observer_allowed(request):
                return _observer_not_found()
            assert trace_store is not None
            records = trace_store.records(session_id)
            if not records:
                return _observer_not_found()
            return {"session_id": session_id, "events": records}

    @app.post("/practice")
    def start_practice(
        field: Annotated[str, Form(min_length=2, max_length=160, pattern=r".*\S.*")],
        work_description: Annotated[str | None, Form(max_length=2_000)] = None,
        difficulty_level: Annotated[int, Form(ge=1, le=10)] = 1,
    ) -> RedirectResponse:
        session = service.start(field.strip(), _optional_text(work_description), difficulty_level)
        return RedirectResponse(
            url=f"/practice/{session.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/practice/{session_id}")
    def practice_page(request: Request, session_id: str):
        session = service.get_session(session_id)
        return templates.TemplateResponse(
            request, "dojo_practice.html", {"session": _practice_page_context(session)}
        )

    @app.post("/practice/{session_id}/responses")
    def practice_response(
        session_id: str,
        response: Annotated[str, Form(min_length=1, max_length=8_000, pattern=r".*\S.*")],
    ) -> RedirectResponse:
        session = service.respond(session_id, response.strip())
        destination = (
            f"/practice/{session_id}/debrief"
            if session.world.get("completed") or session.world.get("terminal")
            else f"/practice/{session_id}"
        )
        return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/practice/{session_id}/clarifications")
    def practice_clarification(
        session_id: str,
        question: Annotated[str, Form(min_length=1, max_length=2_000, pattern=r".*\S.*")],
    ) -> RedirectResponse:
        service.clarify(session_id, question.strip())
        return RedirectResponse(
            url=f"/practice/{session_id}#decision",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/practice/{session_id}/stop")
    def stop_practice(session_id: str) -> RedirectResponse:
        service.stop(session_id)
        return RedirectResponse(
            url=f"/practice/{session_id}/debrief", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/practice/{session_id}/debrief")
    def practice_debrief_page(request: Request, session_id: str):
        session = service.get_session(session_id)
        debrief = service.get_debrief(session_id)
        return templates.TemplateResponse(
            request,
            "dojo_debrief.html",
            {
                "session": _practice_page_context(session),
                "debrief": _debrief_page_context(debrief),
            },
        )

    return app


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _observer_not_found() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})


def _is_loopback(request: Request) -> bool:
    return bool(request.client) and request.client.host in {
        "127.0.0.1",
        "::1",
        "testclient",
    }


def _request_hostname(request: Request) -> str:
    return _normalize_hostname(request.headers.get("host", ""))


def _normalize_hostname(value: str) -> str:
    candidate = value.strip().casefold()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(f"//{candidate}")
        if parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.path or parsed.query or parsed.fragment:
            return ""
        _ = parsed.port
        return (parsed.hostname or "").rstrip(".")
    except ValueError:
        return ""


def _same_origin(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False

    supplied_url = request.headers.get("origin") or request.headers.get("referer")
    if not supplied_url:
        return True
    try:
        supplied = urlsplit(supplied_url)
        expected = urlsplit(str(request.base_url))
        supplied_port = supplied.port or _default_port(supplied.scheme)
        expected_port = expected.port or _default_port(expected.scheme)
    except ValueError:
        return False
    return (
        supplied.scheme.casefold() == expected.scheme.casefold()
        and (supplied.hostname or "").casefold() == (expected.hostname or "").casefold()
        and supplied_port == expected_port
    )


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme.casefold())


def _request_boundary_response(request: Request, status_code: int) -> JSONResponse:
    if request.url.path == "/observer" or request.url.path.startswith("/api/observer/"):
        return _observer_not_found()
    return JSONResponse(status_code=status_code, content={"detail": "Request rejected"})


def _environment_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed > 0 else default


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


create_app = build_app
build_sidecar = build_app


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
    clarifications = raw["clarifications"]
    developments = scenario["how_it_developed"]
    timeline = [_timeline_context(item, index) for index, item in enumerate(developments)]
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
        "constraints": scenario["immediate_constraints"],
        "first_decision": scenario["first_decision"],
        "situational_update": situational_update,
        "latest_turn": presented_turns[-1] if presented_turns else None,
        "prior_turns": presented_turns[:-1],
        "clarifications": [
            {
                "question": item["question"],
                "answer": item["response"]["answer"],
                "status": item["response"]["status"],
                "evidence": item["response"]["evidence"],
            }
            for item in clarifications
        ],
        "can_clarify": (
            not raw["manually_stopped"]
            and raw["debrief"] is None
            and not world.get("completed")
            and not world.get("terminal")
        ),
        "can_stop": (
            bool(turns)
            and not raw["manually_stopped"]
            and raw["debrief"] is None
            and not world.get("completed")
            and not world.get("terminal")
        ),
        "stopped_early": raw["manually_stopped"],
    }


def _timeline_context(item: object, index: int) -> dict[str, object]:
    if isinstance(item, dict):
        return item
    text = str(item)
    match = re.fullmatch(r"(T-\d+m) · ([^:]+): (.+)", text)
    if match is None:
        return {"time": f"{index + 1:02}", "label": text, "detail": None}
    time_label, label, detail = match.groups()
    return {"time": time_label, "label": label, "detail": detail}


def _practice_turn_context(turn: dict[str, object]) -> dict[str, object]:
    assessment = turn["assessment"]
    assert isinstance(assessment, dict)
    return {
        "response": turn["response"],
        "disposition": assessment["disposition"],
        "response_excerpt": assessment.get("response_excerpt"),
        "interpretation": assessment["interpretation"],
        "recognized_intents": assessment["recognized_intents"],
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
        "score": raw["score"],
        "score_rationale": raw["score_rationale"],
        "what_you_saw": raw["noticed"],
        "what_you_missed": raw["missed"],
        "strong_decisions": raw["strong_decisions"],
        "risky_assumptions": raw["risky_assumptions"],
        "stronger_path": raw["better_path"],
        "expert_approach": raw["expert_approach"],
        "carry_forward": raw["carry_forward"],
        "evidence": raw["evidence"],
    }


def _profile_page_context(profile: dict[str, object], cases: object) -> dict[str, object]:
    raw_cases = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in cases
    ]
    presented_cases = [_portfolio_case_summary(item) for item in raw_cases]
    return {
        "profile": {
            "display_name": profile["display_name"],
            "headline": profile["headline"],
            "bio": profile["bio"],
        },
        "stats": profile["counts"],
        "cases": presented_cases,
    }


def _portfolio_case_summary(raw: dict[str, object]) -> dict[str, object]:
    turns = raw["turns"]
    assert isinstance(turns, list)
    first_response = turns[0]["response"] if turns else ""
    return {
        "id": raw["session_id"],
        "title": raw["title"],
        "field": raw["field"],
        "difficulty_level": raw["difficulty_level"],
        "status": raw["status"],
        "status_label": _status_label(str(raw["status"])),
        "score": raw["score"],
        "summary": raw["briefing"],
        "proposed_solution": _excerpt(str(first_response), 280),
        "turn_count": len(turns),
    }


def _portfolio_case_context(raw: dict[str, object]) -> dict[str, object]:
    turns = raw["turns"]
    assert isinstance(turns, list)
    debrief = raw["debrief"]
    return {
        **_portfolio_case_summary(raw),
        "role": raw["learner_role"],
        "outcome_label": _outcome_label(str(raw["outcome"])),
        "first_decision": raw["first_decision"],
        "constraints": raw["immediate_constraints"],
        "turns": [
            {
                "response": turn["response"],
                "recognized_intents": turn["recognized_intents"],
                "action_label": (
                    str(turn["action_kind"]).replace("_", " ")
                    if turn["action_kind"]
                    else "No world action executed"
                ),
                "disposition_label": str(turn["disposition"]).replace("_", " "),
                "outcome_label": _outcome_label(str(turn["outcome"])),
                "strength": turn["strength"],
                "risk": turn["risk"],
            }
            for turn in turns
        ],
        "debrief": (
            {
                "score_rationale": debrief["score_rationale"],
                "strong_decisions": debrief["strong_decisions"],
                "growth_areas": [*debrief["missed"], *debrief["risky_assumptions"]],
                "evidence": debrief["evidence"],
            }
            if isinstance(debrief, dict)
            else None
        ),
    }


def _status_label(status_value: str) -> str:
    return {
        "active": "In progress",
        "resolved": "Resolved",
        "reviewed": "Reviewed",
    }.get(status_value, status_value.replace("_", " ").title())


def _outcome_label(outcome: str) -> str:
    return {
        "active": "Situation still active",
        "recovered": "Recovered",
        "terminal_escalation": "Terminal escalation",
    }.get(outcome, outcome.replace("_", " ").title())


def _excerpt(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"
