"""Billing operations."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from custops.domain.enums import BillingCycle, InvoiceStatus, SubscriptionStatus
from custops.domain.models.billing import Discount, Invoice, Plan, Subscription
from custops.domain.rules.pricing import ProrationInput, ProrationResult, calculate_proration

# Statuses that mean "this invoice still owes money".
UNSETTLED_INVOICE_STATUSES = (InvoiceStatus.OPEN, InvoiceStatus.UNCOLLECTIBLE)


async def get_plan_by_code(session: AsyncSession, code: str) -> Plan | None:
    statement = select(Plan).where(Plan.code == code)
    return (await session.execute(statement)).scalar_one_or_none()


async def list_plans(session: AsyncSession, *, active_only: bool = True) -> list[Plan]:
    statement = select(Plan).order_by(Plan.rank)
    if active_only:
        statement = statement.where(Plan.is_active.is_(True))
    return list((await session.execute(statement)).scalars())


async def get_subscription(
    session: AsyncSession, subscription_id: uuid.UUID
) -> Subscription | None:
    statement = (
        select(Subscription)
        .where(Subscription.id == subscription_id)
        .options(selectinload(Subscription.plan))
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def get_active_subscription(
    session: AsyncSession, account_id: uuid.UUID
) -> Subscription | None:
    """The subscription an upgrade would act on.

    Restricted to ``active``: a cancelled subscription is history, and acting on
    one would produce a technically successful, commercially wrong outcome.
    """
    statement = (
        select(Subscription)
        .where(
            Subscription.account_id == account_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .options(selectinload(Subscription.plan))
        .order_by(Subscription.started_at.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def count_past_due_invoices(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime
) -> int:
    """Invoices that are unsettled and past their due date.

    "Past due" is computed from ``due_at`` against the supplied clock rather than
    trusting a stored status, so a status that was never updated by a nightly job
    cannot make an account look current when it is not. ``now`` is a parameter,
    not ``datetime.now()``, so the check is reproducible in tests and by the
    Validator.
    """
    statement = (
        select(func.count())
        .select_from(Invoice)
        .where(
            Invoice.account_id == account_id,
            Invoice.status.in_(UNSETTLED_INVOICE_STATUSES),
            Invoice.due_at.is_not(None),
            Invoice.due_at < now,
        )
    )
    return int((await session.execute(statement)).scalar_one())


async def list_invoices(
    session: AsyncSession, account_id: uuid.UUID, limit: int = 20
) -> list[Invoice]:
    statement = (
        select(Invoice)
        .where(Invoice.account_id == account_id)
        .order_by(Invoice.issued_at.desc().nullslast())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars())


async def get_active_discount(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime
) -> Discount | None:
    statement = (
        select(Discount)
        .where(
            Discount.account_id == account_id,
            Discount.starts_at <= now,
            (Discount.ends_at.is_(None)) | (Discount.ends_at > now),
        )
        .order_by(Discount.percent_off.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


def price_plan_change(
    subscription: Subscription,
    target_plan: Plan,
    *,
    effective_at: datetime,
) -> ProrationResult:
    """Price a plan change for an existing subscription.

    A thin adapter: it reads prices off the models and hands plain values to the
    deterministic rule. All arithmetic lives in ``domain/rules/pricing.py`` so the
    Validator can recompute it from evidence alone, without a database session.
    """
    current_unit_price = _unit_price(subscription.plan, subscription.billing_cycle)
    new_unit_price = _unit_price(target_plan, subscription.billing_cycle)

    return calculate_proration(
        ProrationInput(
            current_unit_price=current_unit_price,
            new_unit_price=new_unit_price,
            seats=subscription.seats,
            period_start=subscription.current_period_start,
            period_end=subscription.current_period_end,
            effective_at=effective_at,
            currency=target_plan.currency,
        )
    )


def _unit_price(plan: Plan, billing_cycle: str) -> Decimal:
    if billing_cycle == BillingCycle.ANNUAL:
        return plan.annual_price
    return plan.monthly_price


async def apply_plan_change(
    session: AsyncSession,
    subscription_id: uuid.UUID,
    target_plan_id: uuid.UUID,
) -> Subscription | None:
    """Move a subscription onto a different plan.

    **Mutating — not exposed over HTTP.** Reachable only through the MCP
    ``update_subscription`` tool, which verifies an approval record first
    (decision D9).

    Deliberately narrow: it changes the plan and nothing else. Issuing the
    proration invoice, updating the CRM's cached plan and flipping the
    entitlement in the legacy portal are separate steps, because each one can
    fail independently and the Validator has to be able to tell which did.
    """
    subscription = await session.get(Subscription, subscription_id)
    if subscription is None:
        return None

    subscription.plan_id = target_plan_id
    return subscription
