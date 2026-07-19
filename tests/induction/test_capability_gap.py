from apprentice.induction.capability_gap import (
    EXPENSE_POLICY_OUTPUTS,
    detect_expense_policy_gap,
)


def test_detects_expense_policy_gap() -> None:
    request = detect_expense_policy_gap(
        available_fields=("merchant", "amount", "receipt"),
        required_fields=EXPENSE_POLICY_OUTPUTS,
    )

    assert request is not None
    assert request.name == "lookup_expense_policy"
    assert request.required_outputs == EXPENSE_POLICY_OUTPUTS


def test_no_gap_when_policy_fields_are_available() -> None:
    request = detect_expense_policy_gap(
        available_fields=EXPENSE_POLICY_OUTPUTS,
        required_fields=EXPENSE_POLICY_OUTPUTS,
    )

    assert request is None


def test_does_not_propose_tool_for_unknown_gap() -> None:
    request = detect_expense_policy_gap(
        available_fields=("merchant",),
        required_fields=("unrelated_secret",),
    )

    assert request is None
