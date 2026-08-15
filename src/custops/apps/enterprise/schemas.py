"""Transport schemas for the enterprise read API."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from custops.apps.enterprise.assessment import UpgradeAssessment
from custops.domain.rules.eligibility import Finding


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    rank: int
    monthly_price: Decimal
    annual_price: Decimal
    currency: str
    is_active: bool


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    billing_email: str
    currency: str
    # The CRM's cached plan. Exposed deliberately: a caller comparing this
    # against billing is doing the cross-system check §14 describes.
    current_plan_code: str | None
    last_plan_change_at: datetime | None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_ref: str
    name: str
    industry: str | None
    status: str
    accounts: list[AccountOut] = Field(default_factory=list)


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    billing_cycle: str
    seats: int
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    plan: PlanOut


class ContractOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contract_number: str
    status: str
    starts_at: datetime
    ends_at: datetime
    auto_renew: bool
    upgrade_restriction: str
    notice_period_days: int


class SupportSummaryOut(BaseModel):
    total_tickets: int
    unresolved_tickets: int
    open_urgent_tickets: int
    average_satisfaction: float | None


class FindingOut(BaseModel):
    code: str
    message: str
    evidence_ref: str | None

    @classmethod
    def from_finding(cls, finding: Finding) -> FindingOut:
        return cls(
            code=str(finding.code),
            message=finding.message,
            evidence_ref=finding.evidence_ref,
        )


class ProrationOut(BaseModel):
    unused_credit: Decimal
    new_plan_charge: Decimal
    amount_due: Decimal
    days_remaining: int
    days_in_period: int
    currency: str
    breakdown: dict[str, str]


class UpgradeAssessmentOut(BaseModel):
    """The full verdict, with its evidence.

    Everything a human needs to review the decision, and nothing a model
    reasoned privately (Rule 18): findings carry source references, not
    narration.
    """

    account_id: uuid.UUID
    customer_ref: str
    current_plan_code: str
    target_plan_code: str

    eligible: bool
    requires_approval: bool
    can_proceed_automatically: bool

    blockers: list[FindingOut]
    approvals_required: list[FindingOut]
    warnings: list[FindingOut]
    approval_triggers: list[str]
    approval_reasons: list[str]

    proration: ProrationOut
    evidence: dict[str, object]

    @classmethod
    def from_assessment(cls, assessment: UpgradeAssessment) -> UpgradeAssessmentOut:
        return cls(
            account_id=assessment.account_id,
            customer_ref=assessment.customer_ref,
            current_plan_code=assessment.current_plan_code,
            target_plan_code=assessment.target_plan_code,
            eligible=assessment.eligibility.eligible,
            requires_approval=assessment.approval.required,
            can_proceed_automatically=assessment.can_proceed_automatically,
            blockers=[FindingOut.from_finding(f) for f in assessment.eligibility.blockers],
            approvals_required=[
                FindingOut.from_finding(f) for f in assessment.eligibility.approvals_required
            ],
            warnings=[FindingOut.from_finding(f) for f in assessment.eligibility.warnings],
            approval_triggers=[str(t) for t in assessment.approval.triggers],
            approval_reasons=list(assessment.approval.reasons),
            proration=ProrationOut(
                unused_credit=assessment.proration.unused_credit,
                new_plan_charge=assessment.proration.new_plan_charge,
                amount_due=assessment.proration.amount_due,
                days_remaining=assessment.proration.days_remaining,
                days_in_period=assessment.proration.days_in_period,
                currency=assessment.proration.currency,
                breakdown=assessment.proration.breakdown,
            ),
            evidence=dict(assessment.evidence),
        )
