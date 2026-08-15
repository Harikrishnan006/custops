"""What the specialist actually does with a pricing question.

Every read here goes through ``execute_tool`` under ``Role.BILLING_SPECIALIST``
— the same MCP path every other agent uses, with the same permission check and
the same ``tool_calls``/``audit_events`` rows. The specialist being a separate
process does not put it outside the tool boundary; it puts it on the far side of
one, holding its own role.

That has a consequence worth stating plainly: **the specialist can only reason
about what its role can read.** It has no ``get_customer``, so it cannot see
customer or account standing, and no tool exposes negotiated discounts. Its
verdict is scoped accordingly and named accordingly
(``billing_eligible``, ``approval_indicated``). Widening the matrix to make the
answer look more complete would trade a real boundary for a cosmetic one.

The arithmetic is the same deterministic rule the orchestrator runs. The point
is not a second algorithm — it is a second *read*, so a disagreement means the
two sides saw different state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from custops.a2a.contracts.pricing import PricingDecisionRequest, PricingRecommendation
from custops.domain.enums import BillingCycle, UpgradeRestriction
from custops.domain.policies.thresholds import (
    ActionType,
    ApprovalRequest,
    requires_approval,
)
from custops.domain.rules.eligibility import UpgradeContext, check_upgrade_eligibility
from custops.domain.rules.pricing import ProrationInput, calculate_proration
from custops.mcp.permissions.matrix import Role, ToolName
from custops.mcp.tools import enterprise as tools
from custops.mcp.tools.results import ToolResult
from custops.mcp.tools.runtime import ToolContext, execute_tool
from custops.mcp.tools.schemas import AccountInput, GetPricingInput, PlanOutput

PayloadT = TypeVar("PayloadT", bound=BaseModel)

# Statuses the specialist cannot observe through its own role. Passed to the
# eligibility rule as the neutral value so the rule evaluates only the facts the
# specialist genuinely read. The corresponding blockers therefore never fire
# here, and the orchestrator's own assessment remains the authority on them.
_UNOBSERVED_STATUS = "active"


class SpecialistRefusalError(Exception):
    """The specialist can answer, and the answer is 'not from what I can see'."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def price_upgrade(
    session: AsyncSession,
    request: PricingDecisionRequest,
    *,
    now: datetime,
) -> PricingRecommendation:
    """Read billing state through the specialist's own tools, then reason."""
    context = ToolContext(
        session=session,
        role=Role.BILLING_SPECIALIST,
        execution_id=request.execution_id,
        actor_id="billing-specialist",
    )

    subscription = _require(
        await execute_tool(
            context,
            ToolName.GET_SUBSCRIPTION,
            AccountInput(account_id=request.account_id),
            tools.get_subscription,
        ),
        "no_active_subscription",
    )
    current_plan = _require(
        await execute_tool(
            context,
            ToolName.GET_PRICING,
            GetPricingInput(plan_code=subscription.plan_code),
            tools.get_pricing,
        ),
        "current_plan_not_found",
    )
    target_plan = _require(
        await execute_tool(
            context,
            ToolName.GET_PRICING,
            GetPricingInput(plan_code=request.target_plan_code),
            tools.get_pricing,
        ),
        "target_plan_not_found",
    )
    invoices = _require(
        await execute_tool(
            context,
            ToolName.GET_INVOICE,
            AccountInput(account_id=request.account_id),
            tools.get_invoice,
        ),
        "invoices_unavailable",
    )
    # An account with no contract in force is a normal state, not a failure, so
    # a NOT_FOUND here is absorbed rather than refused. Every other tool above
    # returns something the calculation cannot proceed without.
    contract_result = await execute_tool(
        context,
        ToolName.GET_CONTRACT,
        AccountInput(account_id=request.account_id),
        tools.get_contract,
    )
    contract = contract_result.data if contract_result.ok else None

    proration = calculate_proration(
        ProrationInput(
            current_unit_price=_unit_price(current_plan, subscription.billing_cycle),
            new_unit_price=_unit_price(target_plan, subscription.billing_cycle),
            seats=subscription.seats,
            period_start=subscription.current_period_start,
            period_end=subscription.current_period_end,
            effective_at=now,
            currency=target_plan.currency,
        )
    )

    eligibility = check_upgrade_eligibility(
        UpgradeContext(
            # Not readable by this role — see module docstring.
            customer_status=_UNOBSERVED_STATUS,
            account_status=_UNOBSERVED_STATUS,
            subscription_status=subscription.status,
            # Nor is the cancel flag exposed by get_subscription; False keeps the
            # rule from inventing a warning the specialist did not observe.
            subscription_cancel_at_period_end=False,
            current_plan_rank=current_plan.rank,
            target_plan_rank=target_plan.rank,
            target_plan_is_active=target_plan.is_active,
            now=now,
            contract_status=contract.status if contract else None,
            contract_ends_at=contract.ends_at if contract else None,
            contract_upgrade_restriction=(
                contract.upgrade_restriction if contract else UpgradeRestriction.NONE
            ),
            contract_reference=f"contract:{contract.contract_number}" if contract else None,
            past_due_invoice_count=invoices.past_due_count,
        )
    )

    approval = requires_approval(
        ApprovalRequest(
            action=ActionType.SUBSCRIPTION_UPGRADE,
            amount=proration.amount_due,
            # No tool in this role exposes negotiated discounts, so the
            # specialist cannot evaluate the discount threshold at all. Passing
            # None states that honestly; the recommendation is named
            # `approval_indicated` because of exactly this gap.
            discount_percent=None,
            contract_terms_ambiguous=(
                contract is not None
                and contract.upgrade_restriction == UpgradeRestriction.AMBIGUOUS_TERMS
            ),
        )
    )

    blockers = [str(finding.code) for finding in eligibility.blockers]
    triggers = [str(trigger) for trigger in approval.triggers]

    return PricingRecommendation(
        account_id=request.account_id,
        current_plan_code=subscription.plan_code,
        target_plan_code=target_plan.code,
        amount_due=proration.amount_due,
        currency=proration.currency,
        unused_credit=proration.unused_credit,
        new_plan_charge=proration.new_plan_charge,
        days_remaining=proration.days_remaining,
        days_in_period=proration.days_in_period,
        billing_eligible=eligibility.eligible,
        billing_blockers=blockers,
        approval_indicated=approval.required,
        approval_triggers=triggers,
        confidence=_confidence(has_contract=contract is not None, blockers=blockers),
        rationale_summary=_summarise(blockers, proration.amount_due, proration.currency),
        evidence_refs=[
            f"subscription:{subscription.id}",
            f"plan:{target_plan.code}",
            *([f"contract:{contract.contract_number}"] if contract else []),
            f"past_due_invoice_count:{invoices.past_due_count}",
        ],
    )


def _require(result: ToolResult[PayloadT], code: str) -> PayloadT:
    """Unwrap a tool result the calculation cannot proceed without.

    Generic so the payload keeps its type: the reasoning below reads real fields
    off these objects, and erasing them to ``object`` would hide a renamed field
    until runtime.
    """
    if not result.ok or result.data is None:
        message = result.error.message if result.error else "Tool returned no data."
        raise SpecialistRefusalError(code, message)
    return result.data


def _unit_price(plan: PlanOutput, billing_cycle: str) -> Decimal:
    """Pick the price for the cycle the subscription is actually billed on."""
    if billing_cycle == BillingCycle.ANNUAL:
        return plan.annual_price
    return plan.monthly_price


def _confidence(*, has_contract: bool, blockers: list[str]) -> float:
    """How well-supported the specialist's own answer is.

    Not a feeling and not a model output: it reports whether the specialist saw
    the inputs its verdict depends on. A blocked upgrade is a confident 'no'; a
    clean upgrade priced without a contract in view is a slightly weaker 'yes',
    because the contract is where upgrade restrictions live.
    """
    if blockers:
        return 1.0
    return 1.0 if has_contract else 0.8


def _summarise(blockers: list[str], amount: Decimal, currency: str) -> str:
    if blockers:
        return f"Billing blockers: {', '.join(blockers)}."
    return f"Prorated to {amount} {currency} for the remainder of the current period."


__all__ = ["SpecialistRefusalError", "price_upgrade"]
