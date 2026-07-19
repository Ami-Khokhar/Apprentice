from __future__ import annotations

from pathlib import Path

import pytest

from apprentice.induction.normalize import (
    InsufficientDemonstrationsError,
    align_input_fields,
    build_variance_evidence,
)
from apprentice.recorder.artifacts import load_artifact

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "expense"


def _training_demonstrations() -> dict[str, dict]:
    return {
        "training_1": load_artifact(FIXTURES / "training_1"),
        "training_2": load_artifact(FIXTURES / "training_2"),
    }


def _field_by_css(evidence: list[dict], css: str) -> dict:
    for field in evidence:
        if field["anchor"]["css"] == css:
            return field
    raise AssertionError(f"no evidence field for {css!r}")


def test_build_variance_evidence_rejects_fewer_than_two_training_demonstrations() -> None:
    with pytest.raises(InsufficientDemonstrationsError):
        build_variance_evidence({"training_1": load_artifact(FIXTURES / "training_1")})


def test_zero_training_demonstrations_is_also_rejected() -> None:
    with pytest.raises(InsufficientDemonstrationsError):
        build_variance_evidence({})


def test_varying_merchant_and_amount_are_flagged_as_varying_and_always_present() -> None:
    evidence = build_variance_evidence(_training_demonstrations())

    merchant = _field_by_css(evidence, "#merchant")
    amount = _field_by_css(evidence, "#amount")

    assert merchant["varies"] is True
    assert merchant["always_present"] is True
    assert merchant["values_by_demonstration"] == {
        "training_1": "Acme Supplies",
        "training_2": "Atlas Travel",
    }
    assert amount["varies"] is True
    assert amount["always_present"] is True
    assert amount["values_by_demonstration"] == {
        "training_1": "42.50",
        "training_2": "1250.00",
    }


def test_justification_is_conditional_not_always_present_and_does_not_vary() -> None:
    evidence = build_variance_evidence(_training_demonstrations())

    justification = _field_by_css(evidence, "#justification")

    assert justification["always_present"] is False
    assert justification["conditional"] is True
    assert justification["values_by_demonstration"] == {
        "training_1": None,
        "training_2": "Client onsite travel required manager justification.",
    }
    # Only one distinct non-empty value is present, so this is not "varying" text.
    assert justification["varies"] is False


def test_receipt_upload_is_present_in_every_demonstration_and_does_not_vary() -> None:
    evidence = build_variance_evidence(_training_demonstrations())

    receipt = _field_by_css(evidence, "#receipt")

    assert receipt["action"] == "upload"
    assert receipt["always_present"] is True
    assert receipt["varies"] is False
    assert receipt["values_by_demonstration"] == {
        "training_1": "receipt.pdf",
        "training_2": "receipt.pdf",
    }


def test_evidence_field_order_is_deterministic_across_calls() -> None:
    demonstrations = _training_demonstrations()

    first = build_variance_evidence(demonstrations)
    second = build_variance_evidence(demonstrations)

    assert [field["anchor"]["css"] for field in first] == [
        field["anchor"]["css"] for field in second
    ]
    assert [field["anchor"]["css"] for field in first] == [
        "#merchant",
        "#amount",
        "#receipt",
        "#justification",
    ]


def test_align_input_fields_only_considers_input_and_upload_actions() -> None:
    demonstrations = _training_demonstrations()

    fields = align_input_fields(demonstrations)

    assert all(field.action in ("input", "upload") for field in fields)
    assert not any(field.anchor["css"] == "button" for field in fields)
