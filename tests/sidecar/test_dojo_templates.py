from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = Path(__file__).resolve().parents[2] / "apprentice" / "sidecar" / "templates"


def _environment() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined)


def _session() -> dict[str, object]:
    assessment = {
        "disposition": "partially_effective",
        "response": "Pause new work, then inspect worker leases.",
        "response_excerpt": "Pause new work",
        "interpretation": "Contain demand before changing capacity.",
        "recognized_intents": ("Pause new work.", "Inspect worker leases."),
        "strength": "Limits additional checkout failures.",
        "risk": "The queue remains elevated while diagnosis continues.",
        "evidence": ({"source": "metric", "ref": "queue_depth"},),
        "coaching_question": None,
        "action": "pause_dispatch",
        "outcome": "active",
    }
    return {
        "id": "practice-1",
        "stage": "decision",
        "title": "The queue is climbing",
        "summary": "Checkout jobs are timing out during a traffic spike.",
        "timeline": (
            {"time": "18:30", "label": "Traffic rose"},
            {"time": "18:42", "label": "Worker leases stopped releasing"},
        ),
        "role": "You are the incident lead.",
        "constraints": ("Protect active checkouts.", "Avoid duplicate work."),
        "first_decision": "What will you do first?",
        "situational_update": "The queue continues to climb.",
        "latest_turn": assessment,
        "prior_turns": (assessment,),
        "clarifications": (
            {
                "question": "What is the queue depth?",
                "answer": "The queue depth is 1,200 jobs.",
                "status": "answered",
                "evidence": ({"source": "metric", "ref": "queue_depth"},),
            },
        ),
        "can_clarify": True,
        "can_stop": True,
        "stopped_early": False,
    }


def test_practice_template_renders_latest_coaching_and_collapses_history() -> None:
    html = _environment().get_template("dojo_practice.html").render(session=_session())

    assert "Your last decision" in html
    assert "Pause new work" in html
    assert "Interpretation" in html
    assert "Assessment · partially effective" in html
    assert "Plan recognized" in html
    assert "Limits additional checkout failures." in html
    assert "queue_depth" in html
    assert "<details class=\"prior-turns\">" in html
    assert "Commit decision" in html
    assert "End simulation and review" in html
    assert "This is final" in html
    assert "The queue continues to climb." in html
    assert "Ask a clarification" in html
    assert "will not suggest actions, evaluate choices, or give hints" in html
    assert "What is the queue depth?" in html
    assert "The queue depth is 1,200 jobs." in html
    assert 'action="/practice/practice-1/clarifications"' in html
    assert "Write what you would do first and why." not in html


def test_practice_template_does_not_invent_a_remaining_risk() -> None:
    session = _session()
    session["latest_turn"] = {**session["latest_turn"], "disposition": "accepted", "risk": None}
    session["prior_turns"] = ()

    html = _environment().get_template("dojo_practice.html").render(session=session)

    assert "Assessment · accepted" in html
    assert "Remaining risk" not in html


def test_briefing_uses_clear_labels_and_one_first_decision_prompt() -> None:
    session = _session()
    session["stage"] = "briefing"
    session["latest_turn"] = None
    session["prior_turns"] = ()
    session["situational_update"] = None
    session["can_stop"] = False
    session["can_clarify"] = True
    session["timeline"] = (
        {
            "time": "T-15m",
            "label": "Worker leases stopped releasing",
            "detail": "Healthy replacement workers cannot start.",
        },
    )

    html = _environment().get_template("dojo_practice.html").render(session=session)

    assert '<h2 class="briefing-section-title" id="what-happened-title">What happened</h2>' in html
    assert 'aria-labelledby="what-happened-title"' in html
    assert "Your role" in html
    assert "What matters now" in html
    assert "<li>Protect active checkouts.</li>" in html
    assert "<li>Avoid duplicate work.</li>" in html
    assert "Protect active checkouts. · Avoid duplicate work." not in html
    assert "Healthy replacement workers cannot start." in html
    assert html.count("What will you do first?") == 1
    assert "What would you do next—and why?" not in html
    assert "Write what you would do first and why." in html
    assert html.count(str(session["summary"])) == 1
    assert "End simulation and review" not in html


def test_error_template_uses_calm_return_action_contract() -> None:
    html = _environment().get_template("dojo_error.html").render(
        status=404,
        title="This practice could not be found",
        message="The session may have expired or the link may be incomplete.",
        return_href="/",
        return_label="Return to the dojo",
    )

    assert "This practice could not be found" in html
    assert "The session may have expired" in html
    assert 'href="/"' in html
    assert "Return to the dojo" in html


def test_debrief_template_prominently_scores_a_manually_stopped_session() -> None:
    debrief = {
        "score": 74,
        "score_rationale": "You contained demand but did not verify the lease failure.",
        "what_you_saw": ("The queue was growing.",),
        "what_you_missed": ("The lease pattern.",),
        "strong_decisions": ("You limited new dispatches.",),
        "risky_assumptions": ("Workers were merely slow.",),
        "stronger_path": ("Pause dispatch.", "Inspect leases."),
        "expert_approach": "Stabilize, verify, then recover.",
        "carry_forward": "Contain before you repair.",
        "evidence": ({"source": "metric", "ref": "queue_depth"},),
    }

    html = _environment().get_template("dojo_debrief.html").render(
        session={"stopped_early": True}, debrief=debrief
    )

    assert "74<span>/100</span>" in html
    assert debrief["score_rationale"] in html
    assert "before ending the simulation" in html


def test_entry_template_keeps_progressive_form_contract() -> None:
    html = _environment().get_template("dojo_entry.html").render()

    assert 'method="post" action="/practice"' in html
    assert 'name="field"' in html
    assert 'name="work_description"' in html
    assert 'name="difficulty_level"' in html
    assert 'value="1" selected' in html
    assert 'value="10"' in html
    assert "no practical experience" in html
    assert "~15 years" in html
    assert "Enter the dojo" in html
    assert 'data-loading-title="Preparing your situation"' in html


def test_profile_templates_keep_proposal_execution_and_outcome_distinct() -> None:
    profile = {
        "display_name": "Ami",
        "headline": "Engineer practicing incident leadership",
        "bio": "",
    }
    cases = (
        {
            "id": "practice-1",
            "title": "The queue is climbing",
            "field": "Software operations",
            "difficulty_level": 4,
            "status": "resolved",
            "status_label": "Resolved",
            "score": 82,
            "summary": "Checkout jobs are timing out.",
            "proposed_solution": "Pause new work, then inspect the leases.",
            "turn_count": 2,
        },
    )
    profile_html = _environment().get_template("dojo_profile.html").render(
        profile=profile,
        stats={"encountered": 1, "resolved": 1, "reviewed": 0, "active": 0},
        cases=cases,
    )
    case_html = _environment().get_template("dojo_case.html").render(
        case={
            **cases[0],
            "role": "Incident commander",
            "outcome_label": "Recovered",
            "first_decision": "What do you do first?",
            "constraints": ("Protect active checkouts.",),
            "turns": (
                {
                    "response": "Pause new work, then inspect the leases.",
                    "recognized_intents": ("Pause new work", "Inspect leases"),
                    "action_label": "pause dispatch",
                    "disposition_label": "accepted",
                    "outcome_label": "Recovered",
                    "strength": "Contained demand.",
                    "risk": None,
                },
            ),
            "debrief": None,
        }
    )

    assert "Problem portfolio" in profile_html
    assert "82/100" in profile_html
    assert 'action="/profile"' in profile_html
    assert "What you proposed—and what happened" in case_html
    assert "Executed action" in case_html
    assert "Resulting state" in case_html
    assert "not verified workplace experience" in case_html


def test_llm_forms_share_accessible_branded_loading_contract() -> None:
    environment = _environment()
    entry = environment.get_template("dojo_entry.html").render()
    practice = environment.get_template("dojo_practice.html").render(session=_session())

    for html in (entry, practice):
        assert 'id="loading-screen"' in html
        assert 'class="main-surface" data-main-surface' in html
        assert html.index('<main id="main-content">') < html.index('id="loading-screen"')
        assert html.index('id="loading-screen"') < html.index("</main>")
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'src="/static/dojo.js"' in html

    assert 'data-loading-title="Preparing your situation"' in entry
    assert 'data-loading-title="Considering your decision"' in practice


def test_loading_overlay_blurs_only_the_main_surface() -> None:
    css = (TEMPLATES.parent / "static" / "dojo.css").read_text()

    assert "inset: 72px 0 0;" in css
    assert "body.is-loading .main-surface" in css
    assert "filter: blur(6px);" in css
