from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from apprentice.database import SQLiteDatabase
from apprentice.incident.generated import GeneratedScenarioSpec
from apprentice.practice import (
    ClarificationExchange,
    ClarificationReference,
    ClarificationResponse,
    DecisionAssessment,
    EvidenceReference,
    FinalDebrief,
    InvalidPracticeResponseError,
    JudgmentProfile,
    JudgmentProfileCounts,
    LearnerProfile,
    PortfolioCase,
    PortfolioTurn,
    PracticeNotReadyForDebriefError,
    PracticeSession,
    PracticeSessionNotFoundError,
    PracticeTurn,
    ScenarioBlueprint,
)
from apprentice.practice.codex_runner import (
    CodexAuthenticationError,
    CodexCLIUnavailableError,
    CodexExecutionError,
    CodexOutputError,
    CodexStructuredRunner,
    CodexTimeoutError,
)
from apprentice.practice.service import PracticeService
from apprentice.sidecar.app import _practice_page_context, build_app, build_sidecar


def _session(*, completed: bool = False, with_turn: bool = False) -> PracticeSession:
    now = time.time()
    turns = (
        (
            PracticeTurn(
                response="I would stabilize the queue before restarting workers.",
                action_kind="pause_dispatch",
                assessment=DecisionAssessment(
                    response_excerpt="stabilize the queue",
                    interpretation="Stabilize demand before changing capacity.",
                    strength="Limits further damage.",
                    risk="The queue remains elevated.",
                    evidence=(EvidenceReference(source="metric", ref="queue_depth"),),
                ),
                event_count=1,
                outcome="recovered" if completed else "active",
            ),
        )
        if completed or with_turn
        else ()
    )
    return PracticeSession.model_construct(
        id="practice-1",
        profile=LearnerProfile(field="Software operations", work_context="On-call engineer"),
        scenario=ScenarioBlueprint(
            scenario_id="checkout-worker-lease-leak",
            title="The queue is climbing",
            learner_role="Incident commander",
            briefing="Checkout jobs are timing out during a traffic spike.",
            how_it_developed=("Traffic rose", "Worker leases stopped releasing"),
            immediate_constraints=("Protect active checkouts", "Avoid duplicate work"),
            first_decision="What will you do first, and why?",
        ),
        incident_id="incident-1",
        world={
            "completed": completed,
            "terminal": False,
            "events": [{"detail": "The queue continues to climb."}],
            "metrics": {"queue_depth": 1200},
            "artifacts": [],
            "available_actions": [],
            "outcome": "recovered" if completed else "active",
        },
        generated_spec=GeneratedScenarioSpec.model_construct(
            scenario_id="checkout-worker-lease-leak",
            generation_nonce="test-nonce",
            title="The queue is climbing",
            learner_role="Incident commander",
            setting="A checkout operations team during a traffic spike.",
            core_challenge="Restore checkout processing without duplicating customer orders.",
            failure_mechanism="Expired worker leases remain held and block healthy replacements.",
            decision_tradeoff="Fast restarts may duplicate work while waiting extends disruption.",
            briefing="Checkout jobs are timing out during a traffic spike.",
            first_decision="What will you do first, and why?",
            facts=(),
            timeline=(),
            metrics=(),
            artifacts=(),
            actions=(),
            timed_escalations=(),
            success_requirements=(),
            rubric=(),
        ),
        turns=turns,
        created_at=now,
        updated_at=now,
    )


class FakePracticeService:
    def __init__(self) -> None:
        self.session = _session()
        self.profile = JudgmentProfile(
            display_name="Apprentice learner",
            headline="",
            bio="",
            counts=JudgmentProfileCounts(
                encountered=1, resolved=0, reviewed=0, active=1
            ),
        )
        self.start_args: tuple[str, str | None, int] | None = None
        self.response: str | None = None
        self.question: str | None = None
        self.fail_response = False
        self.not_ready = False
        self.start_error: Exception | None = None
        self.respond_error: Exception | None = None

    def start(
        self, field: str, work_context: str | None = None, difficulty_level: int = 1
    ) -> PracticeSession:
        if self.start_error is not None:
            raise self.start_error
        self.start_args = (field, work_context, difficulty_level)
        return self.session

    def get_session(self, session_id: str) -> PracticeSession:
        if session_id == "missing":
            raise PracticeSessionNotFoundError("Unknown practice session: missing")
        assert session_id == self.session.id
        return self.session

    def respond(self, session_id: str, response: str) -> PracticeSession:
        assert session_id == self.session.id
        if self.respond_error is not None:
            raise self.respond_error
        if self.fail_response:
            raise InvalidPracticeResponseError("That action is not available now")
        self.response = response
        self.session = _session(completed=True)
        return self.session

    def clarify(self, session_id: str, question: str) -> PracticeSession:
        assert session_id == self.session.id
        self.question = question
        exchange = ClarificationExchange(
            question=question,
            response=ClarificationResponse(
                status="answered",
                answer="The queue depth is currently 1,200 jobs.",
                evidence=(
                    ClarificationReference(source="metric", ref="queue_depth"),
                ),
            ),
        )
        self.session = self.session.model_copy(update={"clarifications": (exchange,)})
        return self.session

    def stop(self, session_id: str) -> PracticeSession:
        assert session_id == self.session.id
        if not self.session.turns:
            raise InvalidPracticeResponseError("Make at least one decision first")
        self.session = self.session.model_copy(update={"manually_stopped": True})
        return self.session

    def get_debrief(self, session_id: str) -> FinalDebrief:
        assert session_id == self.session.id
        if self.not_ready:
            raise PracticeNotReadyForDebriefError("The scenario is still active")
        return FinalDebrief(
            score=74,
            score_rationale="You contained demand but did not verify the lease failure.",
            noticed=("The queue was still growing.",),
            missed=("The lease expiry pattern.",),
            strong_decisions=("Limited new dispatches.",),
            risky_assumptions=("Assumed workers were merely slow.",),
            better_path=("Pause dispatch", "Inspect leases", "Recover capacity"),
            expert_approach="Stabilize demand, confirm the failure mode, then restore capacity.",
            carry_forward="Contain before you repair.",
            evidence=(EvidenceReference(source="metric", ref="queue_depth"),),
        )

    def get_judgment_profile(self) -> JudgmentProfile:
        return self.profile

    def update_judgment_profile(
        self, display_name: str, headline: str, bio: str
    ) -> JudgmentProfile:
        self.profile = self.profile.model_copy(
            update={
                "display_name": display_name.strip(),
                "headline": headline.strip(),
                "bio": bio.strip(),
            }
        )
        return self.profile

    def list_portfolio_cases(self) -> tuple[PortfolioCase, ...]:
        return (self.get_portfolio_case(self.session.id),)

    def get_portfolio_case(self, session_id: str) -> PortfolioCase:
        session = self.get_session(session_id)
        return PortfolioCase(
            session_id=session.id,
            title=session.scenario.title,
            field=session.profile.field,
            work_context=session.profile.work_context,
            difficulty_level=session.profile.difficulty_level,
            learner_role=session.scenario.learner_role,
            briefing=session.scenario.briefing,
            immediate_constraints=session.scenario.immediate_constraints,
            first_decision=session.scenario.first_decision,
            status="resolved" if session.world["completed"] else "active",
            outcome=session.world["outcome"],
            turns=tuple(
                PortfolioTurn(
                    response=turn.response,
                    action_kind=turn.action_kind,
                    recognized_intents=turn.assessment.recognized_intents,
                    disposition=turn.assessment.disposition,
                    interpretation=turn.assessment.interpretation,
                    strength=turn.assessment.strength,
                    risk=turn.assessment.risk,
                    evidence=turn.assessment.evidence,
                    coaching_question=turn.coaching_question,
                    event_count=turn.event_count,
                    outcome=turn.outcome,
                )
                for turn in session.turns
            ),
            clarifications=session.clarifications,
            evidence=(),
            score=session.debrief.score if session.debrief else None,
            debrief=session.debrief,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


def _client(tmp_path) -> tuple[TestClient, FakePracticeService]:
    service = FakePracticeService()
    database = SQLiteDatabase(tmp_path / "apprentice.db")
    return TestClient(build_app(database=database, practice_service=service)), service


def test_app_defaults_dojo_to_local_codex_without_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    app = build_app(database=SQLiteDatabase(tmp_path / "apprentice.db"))

    assert isinstance(app.state.practice_service, PracticeService)
    assert isinstance(app.state.practice_service._runner, CodexStructuredRunner)
    assert build_sidecar is build_app


@pytest.mark.parametrize(
    "path",
    [
        "/legacy",
        "/console/dashboard",
        "/api/buckets",
        "/api/runs/legacy-run",
        "/api/incidents/legacy-incident",
        "/api/learners/legacy-learner",
    ],
)
def test_legacy_routes_are_not_exposed(tmp_path, path: str) -> None:
    client, _service = _client(tmp_path)

    assert client.get(path).status_code == 404


def test_practice_form_starts_session_and_redirects_to_briefing(tmp_path) -> None:
    client, service = _client(tmp_path)

    response = client.post(
        "/practice",
        data={
            "field": " Software operations ",
            "work_description": " On-call engineer ",
            "difficulty_level": "7",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/practice/practice-1"
    assert service.start_args == ("Software operations", "On-call engineer", 7)


@pytest.mark.parametrize("difficulty_level", [0, 11])
def test_practice_form_rejects_difficulty_outside_scale(
    tmp_path, difficulty_level: int
) -> None:
    client, service = _client(tmp_path)

    response = client.post(
        "/practice",
        data={"field": "Software operations", "difficulty_level": str(difficulty_level)},
    )

    assert response.status_code == 422
    assert service.start_args is None


def test_practice_page_adapts_typed_session_for_minimal_template(tmp_path) -> None:
    client, _service = _client(tmp_path)

    response = client.get("/practice/practice-1")

    assert response.status_code == 200
    assert "The queue is climbing" in response.text
    assert "Checkout jobs are timing out" in response.text
    assert "Your first decision" in response.text
    assert "What will you do first, and why?" in response.text
    assert "Protect active checkouts · Avoid duplicate work" not in response.text


def test_profile_pages_show_cases_and_save_local_identity(tmp_path) -> None:
    client, service = _client(tmp_path)
    service.session = _session(with_turn=True)

    profile = client.get("/profile")
    saved = client.post(
        "/profile",
        data={
            "display_name": "Ami",
            "headline": "Engineer practicing incident leadership",
            "bio": "Learning to make calmer decisions.",
        },
        follow_redirects=False,
    )
    case = client.get("/profile/cases/practice-1")

    assert profile.status_code == 200
    assert "The queue is climbing" in profile.text
    assert "In progress" in profile.text
    assert saved.status_code == 303
    assert saved.headers["location"] == "/profile"
    assert service.profile.display_name == "Ami"
    assert case.status_code == 200
    assert "What you proposed—and what happened" in case.text
    assert "I would stabilize the queue" in case.text
    assert "simulated Apprentice situation" in case.text


def test_profile_json_contract_exposes_counts_and_case_evidence(tmp_path) -> None:
    client, service = _client(tmp_path)
    service.session = _session(with_turn=True)

    profile = client.get("/api/profile")
    case = client.get("/api/profile/cases/practice-1")
    updated = client.put(
        "/api/profile",
        json={"display_name": "Ami", "headline": "Incident learner", "bio": ""},
    )

    assert profile.status_code == 200
    assert profile.json()["profile"]["counts"]["encountered"] == 1
    assert profile.json()["cases"][0]["turns"][0]["response"].startswith("I would")
    assert case.status_code == 200
    assert case.json()["turns"][0]["action_kind"] == "pause_dispatch"
    assert updated.json()["display_name"] == "Ami"


def test_practice_context_structures_timeline_and_constraints() -> None:
    session = _session().model_copy(
        update={
            "scenario": _session().scenario.model_copy(
                update={
                    "how_it_developed": (
                        "T-15m · Forecast published: The forecast moved outside its usual range.",
                        "Legacy event without generated structure",
                    )
                }
            )
        }
    )

    context = _practice_page_context(session)

    assert context["timeline"] == [
        {
            "time": "T-15m",
            "label": "Forecast published",
            "detail": "The forecast moved outside its usual range.",
        },
        {
            "time": "02",
            "label": "Legacy event without generated structure",
            "detail": None,
        },
    ]
    assert context["constraints"] == ["Protect active checkouts", "Avoid duplicate work"]
    assert "constraint" not in context


def test_free_text_response_redirects_to_debrief_when_world_concludes(tmp_path) -> None:
    client, service = _client(tmp_path)

    response = client.post(
        "/practice/practice-1/responses",
        data={"response": " Pause new work, then inspect worker leases. "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/practice/practice-1/debrief"
    assert service.response == "Pause new work, then inspect worker leases."


def test_json_practice_contract_uses_same_service(tmp_path) -> None:
    client, service = _client(tmp_path)

    created = client.post(
        "/api/practice/sessions",
        json={
            "field": "Product management",
            "work_description": "B2B launches",
            "difficulty_level": 4,
        },
    )
    fetched = client.get("/api/practice/sessions/practice-1")
    debrief = client.get("/api/practice/sessions/practice-1/debrief")

    assert created.status_code == 201
    assert created.json()["scenario"]["title"] == "The queue is climbing"
    assert service.start_args == ("Product management", "B2B launches", 4)
    assert fetched.status_code == 200
    assert fetched.json()["profile"]["field"] == "Software operations"
    assert debrief.status_code == 200
    assert debrief.json()["carry_forward"] == "Contain before you repair."
    assert debrief.json()["score"] == 74


def test_clarification_routes_use_same_non_advancing_service_flow(tmp_path) -> None:
    client, service = _client(tmp_path)

    html = client.post(
        "/practice/practice-1/clarifications",
        data={"question": " What is the current queue depth? "},
        follow_redirects=False,
    )

    assert html.status_code == 303
    assert html.headers["location"] == "/practice/practice-1#decision"
    assert service.question == "What is the current queue depth?"

    api = client.post(
        "/api/practice/sessions/practice-1/clarifications",
        json={"question": "Is that measured in jobs?"},
    )

    assert api.status_code == 200
    assert api.json()["clarifications"][0]["response"]["status"] == "answered"
    assert service.question == "Is that measured in jobs?"


def test_manual_stop_routes_freeze_and_redirect_to_debrief(tmp_path) -> None:
    client, service = _client(tmp_path)
    service.session = _session(with_turn=True)

    html = client.post(
        "/practice/practice-1/stop", follow_redirects=False
    )

    assert html.status_code == 303
    assert html.headers["location"] == "/practice/practice-1/debrief"
    assert service.session.manually_stopped is True

    service.session = _session(with_turn=True)
    api = client.post("/api/practice/sessions/practice-1/stop")

    assert api.status_code == 200
    assert api.json()["manually_stopped"] is True


@pytest.mark.parametrize("difficulty_level", [0, 11])
def test_json_practice_contract_rejects_difficulty_outside_scale(
    tmp_path, difficulty_level: int
) -> None:
    client, service = _client(tmp_path)

    response = client.post(
        "/api/practice/sessions",
        json={"field": "Software operations", "difficulty_level": difficulty_level},
    )

    assert response.status_code == 422
    assert service.start_args is None


def test_practice_context_exposes_assessment_without_private_reasoning() -> None:
    first = _session(completed=True)
    first_turn = first.turns[0]
    second_turn = first_turn.model_copy(
        update={"response": "Inspect lease expiry evidence before restoring capacity."}
    )
    session = first.model_copy(update={"turns": (first_turn, second_turn)})

    context = _practice_page_context(session)

    assert context["latest_turn"] == {
        "response": "Inspect lease expiry evidence before restoring capacity.",
        "disposition": "partially_effective",
        "response_excerpt": "stabilize the queue",
        "interpretation": "Stabilize demand before changing capacity.",
        "recognized_intents": [],
        "strength": "Limits further damage.",
        "risk": "The queue remains elevated.",
        "evidence": [{"source": "metric", "ref": "queue_depth"}],
        "coaching_question": None,
        "action": "pause_dispatch",
        "outcome": "recovered",
    }
    assert len(context["prior_turns"]) == 1
    assert "private_reasoning" not in context["latest_turn"]


def test_practice_context_only_offers_manual_stop_during_active_session() -> None:
    active = _practice_page_context(_session(with_turn=True))
    completed = _practice_page_context(_session(completed=True))
    terminal_session = _session(with_turn=True).model_copy(
        update={
            "world": {
                **_session(with_turn=True).world,
                "terminal": True,
                "outcome": "terminal_escalation",
            }
        }
    )
    stopped = _practice_page_context(
        _session(with_turn=True).model_copy(update={"manually_stopped": True})
    )

    assert active["can_stop"] is True
    assert completed["can_stop"] is False
    assert _practice_page_context(terminal_session)["can_stop"] is False
    assert stopped["can_stop"] is False
    assert active["can_clarify"] is True
    assert completed["can_clarify"] is False
    assert _practice_page_context(terminal_session)["can_clarify"] is False
    assert stopped["can_clarify"] is False


def test_html_practice_errors_render_calm_recovery_page(tmp_path) -> None:
    client, service = _client(tmp_path)

    missing = client.get("/practice/missing")
    service.fail_response = True
    invalid = client.post("/practice/practice-1/responses", data={"response": "Restart everything"})
    service.not_ready = True
    premature = client.get("/practice/practice-1/debrief")

    assert missing.status_code == 404
    assert "This practice could not be found" in missing.text
    assert invalid.status_code == 422
    assert "That decision needs another look" in invalid.text
    assert premature.status_code == 409
    assert "The situation is still unfolding" in premature.text


def test_api_practice_errors_keep_structured_json_contract(tmp_path) -> None:
    client, service = _client(tmp_path)

    missing = client.get("/api/practice/sessions/missing")
    service.fail_response = True
    invalid = client.post(
        "/api/practice/sessions/practice-1/responses", json={"response": "Restart everything"}
    )
    service.not_ready = True
    premature = client.get("/api/practice/sessions/practice-1/debrief")

    assert missing.json()["error"]["code"] == "unknown_practice_session"
    assert invalid.json()["error"]["code"] == "invalid_practice_response"
    assert premature.json()["error"]["code"] == "practice_not_complete"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (CodexCLIUnavailableError("secret stderr"), 503, "codex_unavailable"),
        (
            CodexAuthenticationError("secret token and prompt"),
            503,
            "codex_authentication_unavailable",
        ),
        (CodexTimeoutError("secret prompt"), 504, "codex_timeout"),
        (CodexExecutionError("secret stderr"), 502, "codex_execution_failed"),
        (CodexOutputError("secret output"), 502, "codex_output_invalid"),
    ],
)
def test_api_codex_failures_return_safe_structured_errors(
    tmp_path, error: Exception, expected_status: int, expected_code: str
) -> None:
    client, service = _client(tmp_path)
    service.start_error = error

    response = client.post("/api/practice/sessions", json={"field": "Software operations"})

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "secret" not in response.text.casefold()


def test_html_codex_auth_failure_returns_home_with_safe_guidance(tmp_path) -> None:
    client, service = _client(tmp_path)
    service.start_error = CodexAuthenticationError("secret token and stderr")

    response = client.post("/practice", data={"field": "Software operations"})

    assert response.status_code == 503
    assert "practice guide needs authentication" in response.text
    assert "codex login" in response.text
    assert 'href="/"' in response.text
    assert "secret" not in response.text.casefold()


def test_html_codex_timeout_returns_to_current_situation_for_retry(tmp_path) -> None:
    client, service = _client(tmp_path)
    service.respond_error = CodexTimeoutError("secret prompt and stderr")

    response = client.post(
        "/practice/practice-1/responses", data={"response": "Inspect the leases"}
    )

    assert response.status_code == 504
    assert "practice guide took too long" in response.text
    assert 'href="/practice/practice-1"' in response.text
    assert "secret" not in response.text.casefold()
