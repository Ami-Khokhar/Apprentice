from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apprentice.induction.induce import (
    Anchor,
    DecisionPoint,
    InductionValidationError,
    InputSpec,
    Playbook,
    PlaybookStep,
    SuccessCriteria,
    induce_playbook,
    select_training_artifacts,
)
from apprentice.induction.normalize import InsufficientDemonstrationsError
from apprentice.models import Demonstration
from apprentice.recorder.artifacts import load_artifact

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"
EXPECTED_PLAYBOOK_PATH = FIXTURES / "expected_playbook.json"


def _training_demonstrations() -> dict[str, dict]:
    return {
        "training_1": load_artifact(FIXTURES / "training_1"),
        "training_2": load_artifact(FIXTURES / "training_2"),
    }


def _expected_playbook_raw() -> dict:
    return json.loads(EXPECTED_PLAYBOOK_PATH.read_text(encoding="utf-8"))


def _fake_runner_returning(playbook: Playbook):
    def runner(agent, prompt):
        return playbook

    return runner


def test_induce_playbook_rejects_fewer_than_two_training_demonstrations() -> None:
    def runner(agent, prompt):
        raise AssertionError("the model runner must not be invoked without enough demonstrations")

    with pytest.raises(InsufficientDemonstrationsError):
        induce_playbook({"training_1": load_artifact(FIXTURES / "training_1")}, runner=runner)


def test_fake_gpt_output_round_trips_through_the_pydantic_schema() -> None:
    raw = _expected_playbook_raw()

    playbook = Playbook.model_validate(raw)
    dumped = playbook.model_dump(mode="json")

    assert Playbook.model_validate(dumped) == playbook
    assert dumped["task"] == raw["task"]


def test_induce_playbook_accepts_the_expected_playbook_fixture() -> None:
    expected = Playbook.model_validate(_expected_playbook_raw())

    result = induce_playbook(
        _training_demonstrations(),
        runner=_fake_runner_returning(expected),
    )

    assert result == expected


def test_induction_restores_recorded_navigation_without_another_model_call() -> None:
    raw = _expected_playbook_raw()
    raw["steps"] = [step for step in raw["steps"] if step["action"] != "navigate"]
    draft = Playbook.model_validate(raw)

    result = induce_playbook(
        _training_demonstrations(),
        runner=_fake_runner_returning(draft),
    )

    assert [step.action for step in result.steps] == [
        "navigate",
        "fill",
        "fill",
        "fill",
        "upload",
        "commit",
    ]


def test_varying_merchant_and_amount_become_inputs() -> None:
    expected = Playbook.model_validate(_expected_playbook_raw())

    result = induce_playbook(
        _training_demonstrations(),
        runner=_fake_runner_returning(expected),
    )

    input_names = {spec.name for spec in result.inputs}
    assert {"merchant", "amount"} <= input_names


def test_amount_over_policy_branch_becomes_a_decision_point() -> None:
    expected = Playbook.model_validate(_expected_playbook_raw())

    result = induce_playbook(
        _training_demonstrations(),
        runner=_fake_runner_returning(expected),
    )

    assert len(result.decision_points) == 1
    decision = result.decision_points[0]
    assert decision.input_ref == "amount"
    assert decision.activates_input == "justification"
    assert set(decision.supporting_demonstrations) <= {"training_1", "training_2"}
    assert decision.supporting_demonstrations


def test_playbook_conforms_to_the_plan_contract_fields_and_vocabularies() -> None:
    playbook = Playbook.model_validate(_expected_playbook_raw())

    assert playbook.goal
    assert playbook.preconditions
    assert playbook.version == 1
    assert all(
        step.action in ("navigate", "fill", "upload", "click", "commit")
        for step in playbook.steps
    )
    assert all(step.effect in ("observe", "prepare", "commit") for step in playbook.steps)
    assert all(step.intent for step in playbook.steps)


def test_missing_commit_step_fails_playbook_validation() -> None:
    """A recorded mutating request requires at least one effect=commit step."""

    raw = _expected_playbook_raw()
    raw["steps"] = [step for step in raw["steps"] if step["effect"] != "commit"]
    missing_commit = Playbook.model_validate(raw)

    with pytest.raises(InductionValidationError, match="effect=commit"):
        induce_playbook(
            _training_demonstrations(),
            runner=_fake_runner_returning(missing_commit),
        )


def test_commit_step_mislabeled_as_prepare_fails_validation() -> None:
    raw = _expected_playbook_raw()
    for step in raw["steps"]:
        if step["action"] == "commit":
            step["effect"] = "prepare"

    with pytest.raises(ValidationError, match="action/effect"):
        Playbook.model_validate(raw)


def test_commit_effect_cannot_be_carried_by_a_click_action() -> None:
    """A commit step cannot be downgraded to click."""

    raw = _expected_playbook_raw()
    for step in raw["steps"]:
        if step["action"] == "commit":
            step["action"] = "click"

    with pytest.raises(ValidationError, match="action/effect"):
        Playbook.model_validate(raw)


def test_declared_input_unused_by_any_template_or_decision_point_fails() -> None:
    raw = _expected_playbook_raw()
    raw["inputs"].append(
        {
            "name": "cost_center",
            "description": "Never referenced by any step or decision point.",
            "anchor": {"css": "#cost-center", "role": "textbox", "name": "Cost center"},
            "required": False,
        }
    )

    with pytest.raises(ValidationError, match="cost_center"):
        Playbook.model_validate(raw)


def test_model_omitting_a_varying_input_is_rejected() -> None:
    raw = _expected_playbook_raw()
    raw["inputs"] = [spec for spec in raw["inputs"] if spec["name"] != "amount"]
    incomplete = Playbook.model_validate(raw)

    with pytest.raises(InductionValidationError, match="amount"):
        induce_playbook(
            _training_demonstrations(),
            runner=_fake_runner_returning(incomplete),
        )


def test_input_anchor_must_match_evidence_anchor_including_accessible_name() -> None:
    """Anchor matching uses the full (css, role, name) identity, as normalization does."""

    raw = _expected_playbook_raw()
    for spec in raw["inputs"]:
        if spec["name"] == "amount":
            spec["anchor"]["name"] = "Reimbursement total"
    renamed = Playbook.model_validate(raw)

    with pytest.raises(InductionValidationError, match="#amount"):
        induce_playbook(
            _training_demonstrations(),
            runner=_fake_runner_returning(renamed),
        )


def test_decision_point_citing_an_unknown_demonstration_is_rejected() -> None:
    raw = _expected_playbook_raw()
    raw["decision_points"][0]["supporting_demonstrations"] = ["training_9000"]
    fabricated = Playbook.model_validate(raw)

    with pytest.raises(InductionValidationError, match="training_9000"):
        induce_playbook(
            _training_demonstrations(),
            runner=_fake_runner_returning(fabricated),
        )


def test_decision_point_requires_at_least_one_supporting_demonstration() -> None:
    with pytest.raises(ValidationError):
        DecisionPoint(
            condition="amount > 1000",
            input_ref="amount",
            activates_input="justification",
            supporting_demonstrations=(),
        )


def test_training_selection_is_role_based_not_filename_based() -> None:
    """Held-out traces must never feed induction, no matter how paths are named.

    Deliberately mislead any filename-based heuristic: the training-role
    demonstration's artifact_ref looks like a held-out fixture path, and the
    held-out-role demonstration's artifact_ref looks like a training fixture
    path. Only ``role`` may decide inclusion.
    """

    demonstrations = [
        Demonstration(
            id="demo-training",
            bucket_id=1,
            artifact_ref="fixtures/expense/heldout/artifact.json",
            artifact_digest="digest-training",
            role="training",
            created_at=0.0,
        ),
        Demonstration(
            id="demo-heldout",
            bucket_id=1,
            artifact_ref="fixtures/expense/training_2/artifact.json",
            artifact_digest="digest-heldout",
            role="heldout",
            created_at=0.0,
        ),
    ]
    loaded_refs: list[str] = []

    def loader(ref: str) -> dict:
        loaded_refs.append(ref)
        return {"ref": ref}

    result = select_training_artifacts(demonstrations, loader=loader)

    assert set(result) == {"demo-training"}
    assert loaded_refs == ["fixtures/expense/heldout/artifact.json"]


@pytest.mark.live
def test_live_gpt_5_6_induces_a_valid_playbook_from_real_demonstrations() -> None:
    """Exercises the real Agents SDK call against GPT-5.6.

    Requires credentials for APPRENTICE_PROVIDER and network access; excluded from
    the default test run (see pytest markers in pyproject.toml). Run
    explicitly with `uv run pytest -m live` and inspect the resulting
    Agents SDK trace before tuning the induction prompt.
    """

    playbook = induce_playbook(_training_demonstrations())

    assert playbook.task
    assert {"merchant", "amount"} <= {spec.name for spec in playbook.inputs}


def test_playbook_models_construct_directly() -> None:
    anchor = Anchor(css="#merchant", role="textbox", name="Merchant")
    step = PlaybookStep(
        intent="Enter the merchant",
        action="fill",
        anchor=anchor,
        value_template="{merchant}",
        effect="prepare",
    )
    input_spec = InputSpec(name="merchant", description="Merchant name", anchor=anchor)
    success = SuccessCriteria(status_code=201, page_contains="Expense submitted")

    assert step.effect == "prepare"
    assert input_spec.anchor.css == "#merchant"
    assert success.status_code == 201
