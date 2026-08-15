"""Approval thresholds.

The property that matters most here is the one asserted last: a model's stated
confidence can only ever *add* an approval requirement. There is no input by
which model output removes one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from custops.domain.policies.thresholds import (
    ActionType,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalTrigger,
    requires_approval,
)


def test_routine_upgrade_needs_no_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.SUBSCRIPTION_UPGRADE, amount=Decimal("250.00"))
    )

    assert not decision.required
    assert decision.triggers == ()


def test_discount_above_threshold_requires_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.APPLY_DISCOUNT, discount_percent=Decimal("25"))
    )

    assert decision.required
    assert ApprovalTrigger.DISCOUNT_ABOVE_THRESHOLD in decision.triggers


def test_discount_exactly_at_threshold_does_not_require_approval() -> None:
    """The boundary is explicit: 'above threshold' means strictly greater."""
    decision = requires_approval(
        ApprovalRequest(action=ActionType.APPLY_DISCOUNT, discount_percent=Decimal("20.00"))
    )

    assert not decision.required


def test_large_refund_requires_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.ISSUE_REFUND, amount=Decimal("1500.00"))
    )

    assert ApprovalTrigger.REFUND_ABOVE_THRESHOLD in decision.triggers


def test_small_refund_does_not() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.ISSUE_REFUND, amount=Decimal("50.00"))
    )

    assert not decision.required


def test_very_large_charge_requires_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.SUBSCRIPTION_UPGRADE, amount=Decimal("25000.00"))
    )

    assert ApprovalTrigger.CHARGE_ABOVE_THRESHOLD in decision.triggers


@pytest.mark.parametrize(
    "action", [ActionType.SUBSCRIPTION_CANCEL, ActionType.SUBSCRIPTION_DOWNGRADE]
)
def test_destructive_actions_always_require_approval_regardless_of_value(
    action: ActionType,
) -> None:
    """The cost of a wrongly-automated cancellation is not proportional to its price."""
    decision = requires_approval(ApprovalRequest(action=action, amount=Decimal("0.01")))

    assert decision.required
    assert ApprovalTrigger.DESTRUCTIVE_ACTION in decision.triggers


def test_unknown_action_fails_closed() -> None:
    """An action the policy has never heard of is not implicitly safe."""
    decision = requires_approval(ApprovalRequest(action="delete_all_customer_data"))

    assert decision.required
    assert ApprovalTrigger.UNKNOWN_ACTION in decision.triggers


def test_ambiguous_contract_terms_require_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.SUBSCRIPTION_UPGRADE, contract_terms_ambiguous=True)
    )

    assert ApprovalTrigger.CONTRACT_TERMS_AMBIGUOUS in decision.triggers


def test_low_confidence_requires_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.SUBSCRIPTION_UPGRADE, confidence=0.42)
    )

    assert ApprovalTrigger.LOW_CONFIDENCE in decision.triggers


def test_high_confidence_cannot_remove_an_approval_requirement() -> None:
    """The load-bearing property: model output only ever adds requirements."""
    decision = requires_approval(
        ApprovalRequest(
            action=ActionType.SUBSCRIPTION_CANCEL,
            amount=Decimal("5.00"),
            confidence=1.0,
        )
    )

    assert decision.required
    assert ApprovalTrigger.DESTRUCTIVE_ACTION in decision.triggers
    assert ApprovalTrigger.LOW_CONFIDENCE not in decision.triggers


def test_policy_exception_requires_approval() -> None:
    decision = requires_approval(
        ApprovalRequest(action=ActionType.UPDATE_CRM, policy_exception=True)
    )

    assert ApprovalTrigger.POLICY_EXCEPTION in decision.triggers


def test_all_triggers_are_collected_not_just_the_first() -> None:
    decision = requires_approval(
        ApprovalRequest(
            action=ActionType.SUBSCRIPTION_DOWNGRADE,
            amount=Decimal("50000.00"),
            discount_percent=Decimal("40"),
            confidence=0.10,
            contract_terms_ambiguous=True,
            policy_exception=True,
        )
    )

    assert set(decision.triggers) == {
        ApprovalTrigger.DESTRUCTIVE_ACTION,
        ApprovalTrigger.CHARGE_ABOVE_THRESHOLD,
        ApprovalTrigger.DISCOUNT_ABOVE_THRESHOLD,
        ApprovalTrigger.LOW_CONFIDENCE,
        ApprovalTrigger.CONTRACT_TERMS_AMBIGUOUS,
        ApprovalTrigger.POLICY_EXCEPTION,
    }
    assert len(decision.reasons) == len(decision.triggers)


def test_thresholds_are_configurable_without_touching_the_rule() -> None:
    strict = ApprovalPolicy(
        discount_percent_threshold=Decimal("5.00"),
        refund_amount_threshold=Decimal("100.00"),
    )

    decision = requires_approval(
        ApprovalRequest(action=ActionType.APPLY_DISCOUNT, discount_percent=Decimal("10")),
        policy=strict,
    )

    assert decision.required
