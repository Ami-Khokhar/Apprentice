from __future__ import annotations

import json
from pathlib import Path

import pytest

from apprentice.induction.induce import Playbook
from apprentice.induction.shadow import (
    ProposedAction,
    ShadowSubmission,
    evaluate_heldout_shadow,
    record_qualifying_shadow,
)
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.recorder.artifacts import load_artifact
from apprentice.sidecar.run_service import RunService

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"
EVAL_CASES_PATH = FIXTURES / "eval_cases.json"


def _expected_playbook() -> Playbook:
    raw = json.loads((FIXTURES / "expected_playbook.json").read_text(encoding="utf-8"))
    return Playbook.model_validate(raw)


def _eval_cases() -> list[dict]:
    return json.loads(EVAL_CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _submission_from_case(case: dict) -> ShadowSubmission:
    submission = case["submission"]
    return ShadowSubmission(
        actions=tuple(ProposedAction.model_validate(action) for action in submission["actions"]),
        branch_taken=submission["branch_taken"],
        success_status=submission["success_status"],
        success_text=submission["success_text"],
    )


def _fake_runner_returning(submission: ShadowSubmission):
    def runner(agent, prompt):
        return submission

    return runner


@pytest.mark.parametrize("case", _eval_cases(), ids=lambda case: case["id"])
def test_eval_cases_match_expected_pass_fail(case: dict) -> None:
    artifact = load_artifact(FIXTURES / case["artifact"])
    submission = _submission_from_case(case)

    result = evaluate_heldout_shadow(
        _expected_playbook(),
        artifact,
        runner=_fake_runner_returning(submission),
    )

    assert result.passed is case["expected_pass"]


def test_heldout_happy_path_has_no_mismatches() -> None:
    case = next(case for case in _eval_cases() if case["id"] == "heldout_happy_path")
    artifact = load_artifact(FIXTURES / case["artifact"])
    submission = _submission_from_case(case)

    result = evaluate_heldout_shadow(
        _expected_playbook(),
        artifact,
        runner=_fake_runner_returning(submission),
    )

    assert result.passed is True
    assert result.mismatches == ()


def test_navigate_step_without_page_state_does_not_break_prompt_rendering() -> None:
    case = next(case for case in _eval_cases() if case["id"] == "heldout_happy_path")
    artifact = load_artifact(FIXTURES / case["artifact"])
    del artifact["steps"][0]["page_state"]
    submission = _submission_from_case(case)

    result = evaluate_heldout_shadow(
        _expected_playbook(),
        artifact,
        runner=_fake_runner_returning(submission),
    )

    assert result.passed is True


def test_wrong_amount_reports_an_actions_mismatch() -> None:
    case = next(case for case in _eval_cases() if case["id"] == "wrong_amount")
    artifact = load_artifact(FIXTURES / case["artifact"])
    submission = _submission_from_case(case)

    result = evaluate_heldout_shadow(
        _expected_playbook(),
        artifact,
        runner=_fake_runner_returning(submission),
    )

    assert result.passed is False
    assert "actions" in result.mismatches


def test_wrong_branch_reports_a_branch_mismatch() -> None:
    case = next(case for case in _eval_cases() if case["id"] == "wrong_branch")
    artifact = load_artifact(FIXTURES / case["artifact"])
    submission = _submission_from_case(case)

    result = evaluate_heldout_shadow(
        _expected_playbook(),
        artifact,
        runner=_fake_runner_returning(submission),
    )

    assert result.passed is False
    assert "branch" in result.mismatches


def _activate_bucket_with_heldout(repository: Repository):
    bucket = repository.create_bucket(name="file-expense")
    training_1 = load_artifact(FIXTURES / "training_1")
    training_2 = load_artifact(FIXTURES / "training_2")
    heldout = load_artifact(FIXTURES / "heldout")
    repository.add_demonstration(
        bucket.id, "fixtures/expense/training_1", training_1["artifact_digest"], "training"
    )
    repository.add_demonstration(
        bucket.id, "fixtures/expense/training_2", training_2["artifact_digest"], "training"
    )
    repository.add_demonstration(
        bucket.id, "fixtures/expense/heldout", heldout["artifact_digest"], "heldout"
    )
    playbook = _expected_playbook()
    activated = RunService(repository).activate_capability(
        bucket.id, reviewed_playbook=playbook.model_dump(mode="json")
    )
    return activated, playbook, heldout


def test_replaying_the_heldout_trace_twice_produces_one_qualifying_event(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket, playbook, heldout = _activate_bucket_with_heldout(repository)
    service = RunService(repository)
    case = next(case for case in _eval_cases() if case["id"] == "heldout_happy_path")
    runner = _fake_runner_returning(_submission_from_case(case))

    first = record_qualifying_shadow(
        service,
        bucket_id=bucket.id,
        playbook_version=bucket.playbook_version,
        playbook=playbook,
        heldout_artifact=heldout,
        runner=runner,
    )
    second = record_qualifying_shadow(
        service,
        bucket_id=bucket.id,
        playbook_version=bucket.playbook_version,
        playbook=playbook,
        heldout_artifact=heldout,
        runner=runner,
    )

    assert first == second
    assert repository.count_events(bucket.id) == 1
    assert first.evidence_key == f"shadow:{bucket.playbook_version}:{heldout['artifact_digest']}"


def test_record_qualifying_shadow_rejects_a_training_artifact(tmp_path) -> None:
    repository = Repository(tmp_path / "apprentice.db")
    bucket, playbook, _heldout = _activate_bucket_with_heldout(repository)
    service = RunService(repository)
    training_1 = load_artifact(FIXTURES / "training_1")
    case = next(case for case in _eval_cases() if case["id"] == "heldout_happy_path")
    runner = _fake_runner_returning(_submission_from_case(case))

    with pytest.raises(ConflictError, match="held-out"):
        record_qualifying_shadow(
            service,
            bucket_id=bucket.id,
            playbook_version=bucket.playbook_version,
            playbook=playbook,
            heldout_artifact=training_1,
            runner=runner,
        )
