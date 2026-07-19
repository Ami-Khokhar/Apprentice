"""Detect the narrow capability gap used by the policy-expense demo."""

from __future__ import annotations

from apprentice.selftools import CapabilityRequest

EXPENSE_POLICY_OUTPUTS = (
    "category",
    "gl_code",
    "cost_center",
    "approval_required",
    "approval_route",
    "justification_threshold",
)


def detect_expense_policy_gap(
    *, available_fields: tuple[str, ...], required_fields: tuple[str, ...]
) -> CapabilityRequest | None:
    """Request the policy lookup tool when required fields cannot be observed."""
    missing = tuple(field for field in required_fields if field not in available_fields)
    if not missing:
        return None
    if any(field not in EXPENSE_POLICY_OUTPUTS for field in missing):
        return None
    return CapabilityRequest(
        request_id="expense-policy-v1",
        name="lookup_expense_policy",
        reason=(
            "The browser task requires accounting and approval fields that cannot be "
            "derived from the observed expense inputs: " + ", ".join(missing)
        ),
        required_outputs=missing,
    )
