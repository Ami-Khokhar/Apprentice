from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = Path(__file__).resolve().parents[2] / "apprentice" / "sidecar" / "templates"


def _environment() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined)


def _session() -> dict[str, object]:
    assessment = {
        "response": "Pause new work, then inspect worker leases.",
        "response_excerpt": "Pause new work",
        "interpretation": "Contain demand before changing capacity.",
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
        "constraint": "Protect active checkouts.",
        "first_decision": "What will you do first?",
        "situational_update": "The queue continues to climb.",
        "latest_turn": assessment,
        "prior_turns": (assessment,),
    }


def test_practice_template_renders_latest_coaching_and_collapses_history() -> None:
    html = _environment().get_template("dojo_practice.html").render(session=_session())

    assert "Your last decision" in html
    assert "Pause new work" in html
    assert "Interpretation" in html
    assert "Limits additional checkout failures." in html
    assert "queue_depth" in html
    assert "<details class=\"prior-turns\">" in html
    assert "Commit decision" in html


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


def test_entry_template_keeps_progressive_form_contract() -> None:
    html = _environment().get_template("dojo_entry.html").render()

    assert 'method="post" action="/practice"' in html
    assert 'name="field"' in html
    assert 'name="work_description"' in html
    assert "Enter the dojo" in html
    assert 'data-loading-title="Preparing your situation"' in html


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
