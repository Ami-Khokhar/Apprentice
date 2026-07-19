from __future__ import annotations

import json
from pathlib import Path

from apprentice.executor.twin_tools import FileValue, TwinEnvironment
from apprentice.induction.induce import Anchor, Playbook
from apprentice.recorder.artifacts import load_artifact
from apprentice.twin.builder import build_twin
from apprentice.twin.invariants import CHECK_NAMES, evaluate_rehearsal

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"
_RECEIPT_SHA = "78d34c0be3938936c1ea608f35cc9eb85e663b9e3e1954c2e48a18a412580810"


def _expected_playbook() -> Playbook:
    raw = json.loads((FIXTURES / "expected_playbook.json").read_text(encoding="utf-8"))
    return Playbook.model_validate(raw)


def _drive_happy_path(env: TwinEnvironment, *, amount: str) -> None:
    env.navigate("/expense")
    env.fill(Anchor(css="#merchant", role="textbox", name="Merchant"), "Northwind Books")
    env.fill(Anchor(css="#amount", role="spinbutton", name="Amount"), amount)
    env.fill(
        Anchor(css="#justification", role="textbox", name="Justification for expenses over 1000"),
        "",
    )
    env.upload(
        Anchor(css="#receipt", role="button", name="Receipt"),
        FileValue(filename="receipt.pdf", sha256=_RECEIPT_SHA),
    )
    env.commit(Anchor(css="button", role="button", name="Submit expense"))


def _inputs(*, amount: str = "87.25") -> dict:
    return {
        "merchant": "Northwind Books",
        "amount": amount,
        "justification": "",
        "receipt": {"filename": "receipt.pdf", "sha256": _RECEIPT_SHA},
    }


def test_check_names_match_the_binding_six() -> None:
    assert CHECK_NAMES == (
        "action_plan_matches_playbook",
        "commit_payload_matches_inputs",
        "no_undeclared_hosts",
        "success_criteria_met",
        "no_improvised_response_on_critical_path",
        "no_network_egress_for_mutations",
    )


def test_a_complete_correct_run_passes_every_named_check() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")

    _drive_happy_path(env, amount="87.25")
    report = evaluate_rehearsal(
        playbook=playbook, inputs=_inputs(amount="87.25"), twin=twin, actions=env.actions
    )

    assert report.passed is True
    assert set(report.checks) == set(CHECK_NAMES)
    assert all(report.checks.values())


def test_an_incomplete_run_fails_action_plan_matches_playbook_only() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")

    env.navigate("/expense")
    env.fill(Anchor(css="#merchant", role="textbox", name="Merchant"), "Northwind Books")
    # Stops here: never fills amount/justification, never uploads or commits.

    report = evaluate_rehearsal(
        playbook=playbook, inputs=_inputs(), twin=twin, actions=env.actions
    )

    assert report.passed is False
    assert report.checks["action_plan_matches_playbook"] is False
    assert report.checks["no_undeclared_hosts"] is True


def test_mismatched_commit_value_fails_only_commit_payload_matches_inputs() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")

    _drive_happy_path(env, amount="99.99")
    report = evaluate_rehearsal(
        playbook=playbook, inputs=_inputs(amount="87.25"), twin=twin, actions=env.actions
    )

    assert report.passed is False
    assert report.checks["commit_payload_matches_inputs"] is False
    assert report.checks["action_plan_matches_playbook"] is True
    assert report.checks["success_criteria_met"] is True


def test_no_undeclared_hosts_fails_when_visited_hosts_exceed_allowed() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    _drive_happy_path(env, amount="87.25")

    # Simulate a visited host the router recorded that was never declared allowed.
    twin.router.visited_hosts.add("sneaky.example")

    report = evaluate_rehearsal(
        playbook=playbook, inputs=_inputs(amount="87.25"), twin=twin, actions=env.actions
    )

    assert report.checks["no_undeclared_hosts"] is False


def test_missing_file_input_fails_commit_payload_matches_inputs() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    _drive_happy_path(env, amount="87.25")

    wrong_file_inputs = _inputs(amount="87.25")
    wrong_file_inputs["receipt"] = {"filename": "receipt.pdf", "sha256": "0" * 64}

    report = evaluate_rehearsal(
        playbook=playbook, inputs=wrong_file_inputs, twin=twin, actions=env.actions
    )

    assert report.checks["commit_payload_matches_inputs"] is False


def test_extra_undeclared_payload_field_fails_commit_payload_matches_inputs() -> None:
    fixture = load_artifact(FIXTURES / "training_1")
    playbook = _expected_playbook()
    twin = build_twin(fixture, allowed_hosts=["127.0.0.1"], snapshot_urls=())
    env = TwinEnvironment(playbook=playbook, twin=twin, target_base_url="http://127.0.0.1")
    _drive_happy_path(env, amount="87.25")

    # Inputs that fail to account for a payload field the commit carried.
    narrowed_inputs = _inputs(amount="87.25")
    del narrowed_inputs["merchant"]

    report = evaluate_rehearsal(
        playbook=playbook, inputs=narrowed_inputs, twin=twin, actions=env.actions
    )

    assert report.checks["commit_payload_matches_inputs"] is False
