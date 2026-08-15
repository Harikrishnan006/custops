"""Upgrade eligibility rules.

The decision under test is one an LLM must never make. These tests pin it as a
pure function of stated facts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custops.domain.enums import (
    AccountStatus,
    ContractStatus,
    CustomerStatus,
    SubscriptionStatus,
    UpgradeRestriction,
)
from custops.domain.rules.eligibility import (
    ApprovalCode,
    BlockerCode,
    UpgradeContext,
    WarningCode,
    check_upgrade_eligibility,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _context(**overrides: object) -> UpgradeContext:
    defaults: dict[str, object] = {
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "subscription_status": SubscriptionStatus.ACTIVE,
        "subscription_cancel_at_period_end": False,
        "current_plan_rank": 2,
        "target_plan_rank": 3,
        "target_plan_is_active": True,
        "now": NOW,
    }
    defaults.update(overrides)
    return UpgradeContext(**defaults)  # type: ignore[arg-type]


def _codes(findings: tuple[object, ...]) -> set[str]:
    return {finding.code for finding in findings}  # type: ignore[attr-defined]


def test_healthy_account_is_eligible_without_approval() -> None:
    result = check_upgrade_eligibility(_context())

    assert result.eligible
    assert not result.requires_approval
    assert result.blockers == ()


def test_inactive_customer_blocks() -> None:
    result = check_upgrade_eligibility(_context(customer_status=CustomerStatus.CHURNED))

    assert not result.eligible
    assert BlockerCode.CUSTOMER_NOT_ACTIVE in _codes(result.blockers)


def test_suspended_account_blocks() -> None:
    result = check_upgrade_eligibility(_context(account_status=AccountStatus.SUSPENDED))

    assert BlockerCode.ACCOUNT_NOT_ACTIVE in _codes(result.blockers)


def test_past_due_subscription_blocks() -> None:
    result = check_upgrade_eligibility(_context(subscription_status=SubscriptionStatus.PAST_DUE))

    assert BlockerCode.SUBSCRIPTION_NOT_ACTIVE in _codes(result.blockers)


def test_sideways_or_downward_move_is_not_an_upgrade() -> None:
    same = check_upgrade_eligibility(_context(current_plan_rank=3, target_plan_rank=3))
    down = check_upgrade_eligibility(_context(current_plan_rank=3, target_plan_rank=1))

    assert BlockerCode.NOT_AN_UPGRADE in _codes(same.blockers)
    assert BlockerCode.NOT_AN_UPGRADE in _codes(down.blockers)


def test_retired_target_plan_blocks() -> None:
    result = check_upgrade_eligibility(_context(target_plan_is_active=False))

    assert BlockerCode.TARGET_PLAN_INACTIVE in _codes(result.blockers)


def test_past_due_invoices_block() -> None:
    result = check_upgrade_eligibility(_context(past_due_invoice_count=2))

    assert BlockerCode.OUTSTANDING_PAST_DUE_INVOICES in _codes(result.blockers)
    assert "2 past-due invoice(s)" in result.blockers[0].message


def test_every_blocker_is_reported_not_just_the_first() -> None:
    """A human rejecting an upgrade should see all the reasons at once."""
    result = check_upgrade_eligibility(
        _context(
            customer_status=CustomerStatus.INACTIVE,
            account_status=AccountStatus.CLOSED,
            subscription_status=SubscriptionStatus.CANCELLED,
            past_due_invoice_count=1,
        )
    )

    assert _codes(result.blockers) == {
        BlockerCode.CUSTOMER_NOT_ACTIVE,
        BlockerCode.ACCOUNT_NOT_ACTIVE,
        BlockerCode.SUBSCRIPTION_NOT_ACTIVE,
        BlockerCode.OUTSTANDING_PAST_DUE_INVOICES,
    }


class TestContract:
    def test_no_contract_on_file_does_not_block(self) -> None:
        result = check_upgrade_eligibility(_context(contract_status=None))

        assert result.eligible

    def test_term_locked_contract_blocks(self) -> None:
        result = check_upgrade_eligibility(
            _context(
                contract_status=ContractStatus.ACTIVE,
                contract_upgrade_restriction=UpgradeRestriction.TERM_LOCKED,
                contract_reference="contract:CTR-1042",
            )
        )

        assert not result.eligible
        assert BlockerCode.CONTRACT_TERM_LOCKED in _codes(result.blockers)
        assert result.blockers[0].evidence_ref == "contract:CTR-1042"

    def test_expired_contract_blocks(self) -> None:
        result = check_upgrade_eligibility(_context(contract_status=ContractStatus.EXPIRED))

        assert BlockerCode.CONTRACT_NOT_ACTIVE in _codes(result.blockers)

    def test_customer_approval_clause_requires_approval_but_does_not_block(self) -> None:
        result = check_upgrade_eligibility(
            _context(
                contract_status=ContractStatus.ACTIVE,
                contract_upgrade_restriction=UpgradeRestriction.REQUIRES_CUSTOMER_APPROVAL,
            )
        )

        # Eligible and approval-required are independent axes.
        assert result.eligible
        assert result.requires_approval
        assert ApprovalCode.CONTRACT_REQUIRES_CUSTOMER_APPROVAL in _codes(result.approvals_required)

    def test_ambiguous_terms_escalate_to_a_human(self) -> None:
        result = check_upgrade_eligibility(
            _context(
                contract_status=ContractStatus.ACTIVE,
                contract_upgrade_restriction=UpgradeRestriction.AMBIGUOUS_TERMS,
            )
        )

        assert result.requires_approval
        assert ApprovalCode.CONTRACT_TERMS_AMBIGUOUS in _codes(result.approvals_required)

    def test_contract_ending_soon_warns_without_blocking(self) -> None:
        result = check_upgrade_eligibility(
            _context(
                contract_status=ContractStatus.ACTIVE,
                contract_ends_at=NOW + timedelta(days=30),
            )
        )

        assert result.eligible
        assert WarningCode.CONTRACT_ENDING_SOON in _codes(result.warnings)

    def test_distant_contract_end_does_not_warn(self) -> None:
        result = check_upgrade_eligibility(
            _context(
                contract_status=ContractStatus.ACTIVE,
                contract_ends_at=NOW + timedelta(days=365),
            )
        )

        assert result.warnings == ()


class TestWarnings:
    def test_pending_cancellation_warns(self) -> None:
        result = check_upgrade_eligibility(_context(subscription_cancel_at_period_end=True))

        assert result.eligible
        assert WarningCode.SUBSCRIPTION_SET_TO_CANCEL in _codes(result.warnings)

    def test_open_urgent_tickets_warn(self) -> None:
        result = check_upgrade_eligibility(_context(open_urgent_ticket_count=3))

        assert result.eligible
        assert WarningCode.OPEN_URGENT_TICKETS in _codes(result.warnings)


def test_result_is_reproducible_for_identical_input() -> None:
    """The Validator recomputes this; identical facts must give an identical verdict."""
    context = _context(past_due_invoice_count=1, open_urgent_ticket_count=2)

    assert check_upgrade_eligibility(context) == check_upgrade_eligibility(context)
