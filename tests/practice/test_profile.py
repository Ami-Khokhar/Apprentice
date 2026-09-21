from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apprentice.database import SQLiteDatabase
from apprentice.practice.contracts import EvidenceReference
from apprentice.practice.service import PracticeService, PracticeSessionNotFoundError
from tests.practice.test_service import FakeRunner, debrief, decision, generated_spec


def profile_service(path: Path, runner: FakeRunner) -> tuple[PracticeService, SQLiteDatabase]:
    database = SQLiteDatabase(path)
    return PracticeService(database=database, runner=runner), database


def test_profile_identity_defaults_validates_and_persists(tmp_path: Path) -> None:
    service, database = profile_service(tmp_path / "dojo.sqlite3", FakeRunner([generated_spec()]))

    default = service.get_judgment_profile()
    assert default.display_name == "Apprentice learner"
    assert default.headline == ""
    assert default.bio == ""
    assert default.counts.encountered == 0

    updated = service.update_judgment_profile(
        "  Anika Rao  ",
        "  Operations leader  ",
        "  I practice incident judgment.  ",
    )

    assert updated.display_name == "Anika Rao"
    assert updated.headline == "Operations leader"
    assert updated.bio == "I practice incident judgment."
    assert updated.updated_at is not None
    restarted = PracticeService(database=database, runner=FakeRunner([]))
    assert restarted.get_judgment_profile() == updated

    with pytest.raises(ValidationError):
        restarted.update_judgment_profile("   ", "", "")
    assert restarted.get_judgment_profile() == updated


def test_existing_sessions_project_to_cases_with_solutions_and_status_counts(
    tmp_path: Path,
) -> None:
    resolved_response = "Revert the implicated overnight upload change, then validate the release."
    reviewed_response = "Inspect the pipeline before making another change."
    runner = FakeRunner(
        [
            generated_spec(),
            decision(
                "correct-and-reforecast",
                excerpt="Revert the implicated overnight upload change",
                disposition="accepted",
                risk=None,
                prerequisite_override=True,
                override_justification="The implicated change has a known-good prior version.",
                evidence=(EvidenceReference(source="action", ref="correct-and-reforecast"),),
            ),
            debrief(),
            generated_spec(),
            decision(
                "inspect-pipeline",
                excerpt="Inspect the pipeline",
                evidence=(EvidenceReference(source="action", ref="inspect-pipeline"),),
            ),
            generated_spec(),
        ]
    )
    service, database = profile_service(tmp_path / "dojo.sqlite3", runner)

    resolved = service.start("software operations", difficulty_level=7)
    service.respond(resolved.id, resolved_response)
    service.get_debrief(resolved.id)
    reviewed = service.start("supply chain analysis", difficulty_level=4)
    service.respond(reviewed.id, reviewed_response)
    service.stop(reviewed.id)
    active = service.start("engineering leadership", difficulty_level=9)

    restarted = PracticeService(database=database, runner=FakeRunner([]))
    cases = restarted.list_portfolio_cases()
    profile = restarted.get_judgment_profile()

    assert {item.session_id for item in cases} == {resolved.id, reviewed.id, active.id}
    assert profile.counts.model_dump() == {
        "encountered": 3,
        "resolved": 1,
        "reviewed": 1,
        "active": 1,
    }
    resolved_case = restarted.get_portfolio_case(resolved.id)
    assert resolved_case.field == "software operations"
    assert resolved_case.difficulty_level == 7
    assert resolved_case.status == "resolved"
    assert resolved_case.outcome == "recovered"
    assert resolved_case.score == 82
    assert resolved_case.turns[0].response == resolved_response
    assert resolved_case.turns[0].action_kind == "correct-and-reforecast"
    assert resolved_case.turns[0].recognized_intents == ("Take the proposed immediate step.",)
    assert resolved_case.debrief is not None
    assert {(item.source, item.ref) for item in resolved_case.evidence} == {
        ("action", "correct-and-reforecast"),
        ("metric", "stockout-risk"),
    }
    assert restarted.get_portfolio_case(reviewed.id).status == "reviewed"
    assert restarted.get_portfolio_case(reviewed.id).outcome == "active"
    assert restarted.get_portfolio_case(active.id).status == "active"


def test_get_portfolio_case_rejects_an_unknown_session() -> None:
    service = PracticeService(runner=FakeRunner([]))

    with pytest.raises(PracticeSessionNotFoundError, match="missing"):
        service.get_portfolio_case("missing")
