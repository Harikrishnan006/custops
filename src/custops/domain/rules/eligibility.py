"""Upgrade eligibility.

The question this module answers — *may this account move to this plan?* — is
answered in Python, from structured facts, and never by a language model. A model
may **explain** a blocker, retrieve the contract clause behind it, or draft the
message to the customer. It cannot decide the answer, because the decision has
financial and contractual consequences and must be reproducible.

The result distinguishes three outcomes, and the distinction matters:

* **blockers** — the upgrade must not proceed. No approval can wave these
  through from inside the workflow; they need the underlying fact to change.
* **approval_required** — permitted, but only with a recorded human decision
  (BUILD_SPEC §13).
* **warnings** — proceed, but a human reading the audit trail should know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from custops.domain.enums import (
    AccountStatus,
    ContractStatus,
    CustomerStatus,
    SubscriptionStatus,
    UpgradeRestriction,
)


class BlockerCode(StrEnum):
    """Why an upgrade cannot proceed."""

    CUSTOMER_NOT_ACTIVE = "customer_not_active"
    ACCOUNT_NOT_ACTIVE = "account_not_active"
    SUBSCRIPTION_NOT_ACTIVE = "subscription_not_active"
    NOT_AN_UPGRADE = "not_an_upgrade"
    TARGET_PLAN_INACTIVE = "target_plan_inactive"
    CONTRACT_NOT_ACTIVE = "contract_not_active"
    CONTRACT_TERM_LOCKED = "contract_term_locked"
    OUTSTANDING_PAST_DUE_INVOICES = "outstanding_past_due_invoices"


class ApprovalCode(StrEnum):
    """Why a human must decide."""

    CONTRACT_REQUIRES_CUSTOMER_APPROVAL = "contract_requires_customer_approval"
    CONTRACT_TERMS_AMBIGUOUS = "contract_terms_ambiguous"


class WarningCode(StrEnum):
    """Worth recording, not worth stopping for."""

    CONTRACT_ENDING_SOON = "contract_ending_soon"
    OPEN_URGENT_TICKETS = "open_urgent_tickets"
    SUBSCRIPTION_SET_TO_CANCEL = "subscription_set_to_cancel"


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason, with the evidence that produced it."""

    code: str
    message: str
    # Where the fact came from, e.g. "contract:CTR-1042". Lets an approval
    # request cite its sources rather than assert conclusions (BUILD_SPEC §13).
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class UpgradeContext:
    """The structured facts an eligibility decision depends on.

    Deliberately primitives rather than ORM objects: the rule is then a pure
    function of stated inputs, testable exhaustively, and incapable of lazily
    loading something nobody expected. The service layer assembles this from the
    database; Phase 5's Research agent assembles it from retrieved evidence.
    """

    customer_status: str
    account_status: str
    subscription_status: str
    subscription_cancel_at_period_end: bool
    current_plan_rank: int
    target_plan_rank: int
    target_plan_is_active: bool
    now: datetime

    contract_status: str | None = None
    contract_ends_at: datetime | None = None
    contract_upgrade_restriction: str = UpgradeRestriction.NONE
    contract_reference: str | None = None

    past_due_invoice_count: int = 0
    open_urgent_ticket_count: int = 0

    # A contract inside this window is flagged; the upgrade still proceeds.
    contract_ending_soon_days: int = 60


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """The verdict, with everything needed to explain it."""

    blockers: tuple[Finding, ...] = field(default_factory=tuple)
    approvals_required: tuple[Finding, ...] = field(default_factory=tuple)
    warnings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> bool:
        """True when nothing blocks the upgrade.

        Note that this is independent of ``requires_approval``: an upgrade can be
        eligible *and* require a human decision. Collapsing the two would let an
        approval-needing action look like an automatic one.
        """
        return not self.blockers

    @property
    def requires_approval(self) -> bool:
        return bool(self.approvals_required)


def check_upgrade_eligibility(context: UpgradeContext) -> EligibilityResult:
    """Decide whether an account may move to a higher plan.

    Every condition is evaluated — the function does not short-circuit on the
    first blocker. A human reviewing a rejected upgrade should see all of the
    reasons at once, not discover them one round trip at a time.
    """
    blockers: list[Finding] = []
    approvals: list[Finding] = []
    warnings: list[Finding] = []

    if context.customer_status != CustomerStatus.ACTIVE:
        blockers.append(
            Finding(
                code=BlockerCode.CUSTOMER_NOT_ACTIVE,
                message=f"Customer status is '{context.customer_status}', expected 'active'.",
                evidence_ref="customer.status",
            )
        )

    if context.account_status != AccountStatus.ACTIVE:
        blockers.append(
            Finding(
                code=BlockerCode.ACCOUNT_NOT_ACTIVE,
                message=f"Account status is '{context.account_status}', expected 'active'.",
                evidence_ref="account.status",
            )
        )

    if context.subscription_status != SubscriptionStatus.ACTIVE:
        blockers.append(
            Finding(
                code=BlockerCode.SUBSCRIPTION_NOT_ACTIVE,
                message=(
                    f"Subscription status is '{context.subscription_status}', expected 'active'."
                ),
                evidence_ref="subscription.status",
            )
        )

    if context.target_plan_rank <= context.current_plan_rank:
        blockers.append(
            Finding(
                code=BlockerCode.NOT_AN_UPGRADE,
                message=(
                    f"Target plan rank {context.target_plan_rank} is not above the current "
                    f"rank {context.current_plan_rank}; this is not an upgrade."
                ),
                evidence_ref="plan.rank",
            )
        )

    if not context.target_plan_is_active:
        blockers.append(
            Finding(
                code=BlockerCode.TARGET_PLAN_INACTIVE,
                message="Target plan is not available for new subscriptions.",
                evidence_ref="plan.is_active",
            )
        )

    if context.past_due_invoice_count > 0:
        blockers.append(
            Finding(
                code=BlockerCode.OUTSTANDING_PAST_DUE_INVOICES,
                message=(
                    f"{context.past_due_invoice_count} past-due invoice(s) must be settled "
                    "before an upgrade."
                ),
                evidence_ref="invoices.past_due",
            )
        )

    _evaluate_contract(context, blockers, approvals, warnings)

    if context.subscription_cancel_at_period_end:
        warnings.append(
            Finding(
                code=WarningCode.SUBSCRIPTION_SET_TO_CANCEL,
                message="Subscription is set to cancel at period end; confirm intent.",
                evidence_ref="subscription.cancel_at_period_end",
            )
        )

    if context.open_urgent_ticket_count > 0:
        warnings.append(
            Finding(
                code=WarningCode.OPEN_URGENT_TICKETS,
                message=(
                    f"{context.open_urgent_ticket_count} open urgent support ticket(s) on "
                    "this account."
                ),
                evidence_ref="support_tickets.open_urgent",
            )
        )

    return EligibilityResult(
        blockers=tuple(blockers),
        approvals_required=tuple(approvals),
        warnings=tuple(warnings),
    )


def _evaluate_contract(
    context: UpgradeContext,
    blockers: list[Finding],
    approvals: list[Finding],
    warnings: list[Finding],
) -> None:
    """Apply the contract's constraints.

    An account with no contract on file is not blocked — plenty of self-serve
    accounts have none. What is blocked is a contract that exists and forbids
    the change.
    """
    if context.contract_status is None:
        return

    reference = context.contract_reference

    if context.contract_status != ContractStatus.ACTIVE:
        blockers.append(
            Finding(
                code=BlockerCode.CONTRACT_NOT_ACTIVE,
                message=f"Contract status is '{context.contract_status}', expected 'active'.",
                evidence_ref=reference,
            )
        )

    restriction = context.contract_upgrade_restriction

    if restriction == UpgradeRestriction.TERM_LOCKED:
        blockers.append(
            Finding(
                code=BlockerCode.CONTRACT_TERM_LOCKED,
                message=(
                    "Contract locks the plan for the committed term; an amendment is "
                    "required before upgrading."
                ),
                evidence_ref=reference,
            )
        )
    elif restriction == UpgradeRestriction.REQUIRES_CUSTOMER_APPROVAL:
        approvals.append(
            Finding(
                code=ApprovalCode.CONTRACT_REQUIRES_CUSTOMER_APPROVAL,
                message="Contract requires documented customer approval before a tier change.",
                evidence_ref=reference,
            )
        )
    elif restriction == UpgradeRestriction.AMBIGUOUS_TERMS:
        # The model may summarise the clause; it may not decide what it means.
        approvals.append(
            Finding(
                code=ApprovalCode.CONTRACT_TERMS_AMBIGUOUS,
                message=(
                    "Contract terms on tier changes are ambiguous; human interpretation required."
                ),
                evidence_ref=reference,
            )
        )

    if context.contract_ends_at is not None:
        days_remaining = (context.contract_ends_at - context.now).days
        if 0 <= days_remaining <= context.contract_ending_soon_days:
            warnings.append(
                Finding(
                    code=WarningCode.CONTRACT_ENDING_SOON,
                    message=f"Contract ends in {days_remaining} day(s).",
                    evidence_ref=reference,
                )
            )
