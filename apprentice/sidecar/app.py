from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from apprentice.database import SQLiteDatabase
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


def build_app(
    db_path: str | Path | None = None,
    *,
    database: SQLiteDatabase | None = None,
    practice_service: PracticeService | None = None,
) -> FastAPI:
    if database is not None and db_path is not None:
        raise ValueError("Pass either database or db_path, not both")

    database = database or SQLiteDatabase(db_path or "apprentice.db")
    service = practice_service or PracticeService(database=database)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app = FastAPI(title="Apprentice")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.state.database = database
    app.state.practice_service = service

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

    @app.post("/api/practice/sessions/{session_id}/stop")
    def stop_practice_session(session_id: str) -> dict[str, object]:
        session = service.stop(session_id)
        return session.model_dump(mode="json")

    @app.get("/api/practice/sessions/{session_id}/debrief")
    def get_practice_debrief(session_id: str) -> dict[str, object]:
        return service.get_debrief(session_id).model_dump(mode="json")

    @app.get("/")
    def home(request: Request):
        return templates.TemplateResponse(request, "dojo_entry.html", {})

    @app.post("/practice")
    def start_practice(
        field: Annotated[str, Form(min_length=2, max_length=160, pattern=r".*\S.*")],
        work_description: Annotated[str | None, Form(max_length=2_000)] = None,
        difficulty_level: Annotated[int, Form(ge=1, le=10)] = 1,
    ) -> RedirectResponse:
        session = service.start(
            field.strip(), _optional_text(work_description), difficulty_level
        )
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
