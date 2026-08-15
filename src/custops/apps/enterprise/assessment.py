"""Upgrade assessment — where stored facts meet deterministic rules.

This module is the bridge the whole architecture depends on. It reads state from
the systems of record, assembles it into the plain-value ``UpgradeContext`` the
rules accept, and returns a verdict with the evidence that produced it.

Nothing here decides anything. Eligibility comes from
``domain/rules/eligibility``, the price from ``domain/rules/pricing``, and the
approval requirement from ``domain/policies/thresholds``. This module's only job
is gathering inputs and reporting outputs — which is why the result can be
recomputed later by the Validator from the same evidence, with no session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from custops.apps.enterprise.billing import service as billing_service
from custops.apps.enterprise.contracts import service as contract_service
from custops.apps.enterprise.crm import service as crm_service
from custops.apps.enterprise.support import service as support_service
from custops.domain.enums import UpgradeRestriction
from custops.domain.policies.thresholds import (
    ActionType,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    requires_approval,
)
from custops.domain.rules.eligibility import (
    EligibilityResult,
    UpgradeContext,
    check_upgrade_eligibility,
)
from custops.domain.rules.pricing import ProrationResult


class AssessmentErrorCode(StrEnum):
    """Failures that prevent an assessment from being made at all.

    Distinct from a *blocked* upgrade: "this account cannot be upgraded because
    its contract forbids it" is an answer, while "there is no such account" means
    the question could not be asked. Conflating them would let a typo look like a
    policy decision.
    """

    ACCOUNT_NOT_FOUND = "account_not_found"
    TARGET_PLAN_NOT_FOUND = "target_plan_not_found"
    NO_ACTIVE_SUBSCRIPTION = "no_active_subscription"


class AssessmentError(Exception):
    """Raised when the assessment cannot be performed."""

    def __init__(self, code: AssessmentErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class UpgradeAssessment:
    """A complete, explainable answer to 'should this upgrade proceed?'"""

    account_id: uuid.UUID
    customer_ref: str
    current_plan_code: str
    target_plan_code: str
    eligibility: EligibilityResult
    proration: ProrationResult
    approval: ApprovalDecision
    evidence: dict[str, Any]

    @property
    def can_proceed_automatically(self) -> bool:
        """Eligible *and* no human decision required.

        Both conditions, deliberately. An eligible upgrade that needs approval is
        not an automatic one, and treating it as such is precisely the failure
        the approval architecture exists to prevent.
        """
        return self.eligibility.eligible and not self.approval.required


async def assess_upgrade(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    target_plan_code: str,
    now: datetime,
    policy: ApprovalPolicy | None = None,
) -> UpgradeAssessment:
    """Gather state, apply the rules, and return a verdict with its evidence.

    ``now`` is injected rather than read from the clock so the assessment is
    reproducible: the Validator re-runs it against the same instant and expects
    the same answer.
    """
    account = await crm_service.get_account(session, account_id)
    if account is None:
        raise AssessmentError(
            AssessmentErrorCode.ACCOUNT_NOT_FOUND, f"No account with id {account_id}."
        )

    target_plan = await billing_service.get_plan_by_code(session, target_plan_code)
    if target_plan is None:
        raise AssessmentError(
            AssessmentErrorCode.TARGET_PLAN_NOT_FOUND,
            f"No plan with code '{target_plan_code}'.",
        )

    subscription = await billing_service.get_active_subscription(session, account_id)
    if subscription is None:
        raise AssessmentError(
            AssessmentErrorCode.NO_ACTIVE_SUBSCRIPTION,
            f"Account {account_id} has no active subscription.",
        )

    contract = await contract_service.get_active_contract(session, account_id, now=now)
    past_due_count = await billing_service.count_past_due_invoices(session, account_id, now=now)
    urgent_tickets = await support_service.count_open_urgent_tickets(session, account_id)
    # An existing negotiated discount carries onto the new plan. A deep discount
    # moving onto a materially larger contract value is a commercial decision, so
    # it is fed to the approval policy rather than silently inherited.
    discount = await billing_service.get_active_discount(session, account_id, now=now)

    context = UpgradeContext(
        customer_status=account.customer.status,
        account_status=account.status,
        subscription_status=subscription.status,
        subscription_cancel_at_period_end=subscription.cancel_at_period_end,
        current_plan_rank=subscription.plan.rank,
        target_plan_rank=target_plan.rank,
        target_plan_is_active=target_plan.is_active,
        now=now,
        contract_status=contract.status if contract else None,
        contract_ends_at=contract.ends_at if contract else None,
        contract_upgrade_restriction=(
            contract.upgrade_restriction if contract else UpgradeRestriction.NONE
        ),
        contract_reference=f"contract:{contract.contract_number}" if contract else None,
        past_due_invoice_count=past_due_count,
        open_urgent_ticket_count=urgent_tickets,
    )

    eligibility = check_upgrade_eligibility(context)

    proration = billing_service.price_plan_change(subscription, target_plan, effective_at=now)

    approval = requires_approval(
        ApprovalRequest(
            action=ActionType.SUBSCRIPTION_UPGRADE,
            amount=proration.amount_due,
            discount_percent=discount.percent_off if discount else None,
            contract_terms_ambiguous=(
                context.contract_upgrade_restriction == UpgradeRestriction.AMBIGUOUS_TERMS
            ),
        ),
        policy=policy,
    )

    return UpgradeAssessment(
        account_id=account_id,
        customer_ref=account.customer.external_ref,
        current_plan_code=subscription.plan.code,
        target_plan_code=target_plan.code,
        eligibility=eligibility,
        proration=proration,
        approval=approval,
        evidence=_build_evidence(
            account_id=account_id,
            subscription_id=subscription.id,
            contract_reference=context.contract_reference,
            past_due_count=past_due_count,
            urgent_tickets=urgent_tickets,
            amount_due=proration.amount_due,
            discount_percent=discount.percent_off if discount else None,
        ),
    )


def _build_evidence(
    *,
    account_id: uuid.UUID,
    subscription_id: uuid.UUID,
    contract_reference: str | None,
    past_due_count: int,
    urgent_tickets: int,
    amount_due: Decimal,
    discount_percent: Decimal | None,
) -> dict[str, Any]:
    """Source references for every fact the verdict rests on.

    References, not copies: an audit reader needs to know *where* a fact came
    from so they can go and check it. Structured, and never chain-of-thought
    (Rule 18).
    """
    return {
        "account": f"account:{account_id}",
        "subscription": f"subscription:{subscription_id}",
        "contract": contract_reference,
        "past_due_invoice_count": past_due_count,
        "open_urgent_ticket_count": urgent_tickets,
        "amount_due": str(amount_due),
        "active_discount_percent": str(discount_percent) if discount_percent else None,
    }
