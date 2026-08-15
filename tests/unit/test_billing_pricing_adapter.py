"""The adapter between billing models and the pricing rule.

Runs without a database: SQLAlchemy models can be instantiated in memory, so the
mapping from stored fields to rule inputs — which unit price a billing cycle
selects, how seats and period boundaries carry through — is verifiable here
rather than only in an integration test that skips.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custops.apps.enterprise.billing.service import price_plan_change
from custops.domain.enums import BillingCycle, SubscriptionStatus
from custops.domain.models.billing import Plan, Subscription
from custops.domain.rules.pricing import PricingError

PERIOD_START = datetime(2026, 3, 1, tzinfo=UTC)
PERIOD_END = PERIOD_START + timedelta(days=30)


def _plan(code: str, rank: int, monthly: str, annual: str) -> Plan:
    return Plan(
        id=uuid.uuid4(),
        code=code,
        name=code.title(),
        rank=rank,
        monthly_price=Decimal(monthly),
        annual_price=Decimal(annual),
        currency="USD",
        features={},
        is_active=True,
    )


def _subscription(plan: Plan, *, seats: int = 1, cycle: str = BillingCycle.MONTHLY) -> Subscription:
    subscription = Subscription(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=cycle,
        seats=seats,
        started_at=PERIOD_START - timedelta(days=365),
        current_period_start=PERIOD_START,
        current_period_end=PERIOD_END,
        cancel_at_period_end=False,
    )
    subscription.plan = plan
    return subscription


PROFESSIONAL = _plan("professional", 2, "299.00", "2990.00")
ENTERPRISE = _plan("enterprise", 3, "999.00", "9990.00")


def test_monthly_cycle_uses_monthly_prices() -> None:
    subscription = _subscription(PROFESSIONAL, seats=10)

    result = price_plan_change(
        subscription, ENTERPRISE, effective_at=PERIOD_START + timedelta(days=15)
    )

    # Half a 30-day period, 10 seats: 299*10/2 credited, 999*10/2 charged.
    assert result.unused_credit == Decimal("1495.00")
    assert result.new_plan_charge == Decimal("4995.00")
    assert result.amount_due == Decimal("3500.00")


def test_annual_cycle_uses_annual_prices() -> None:
    """The cycle selects which stored price is the unit price."""
    subscription = _subscription(PROFESSIONAL, seats=1, cycle=BillingCycle.ANNUAL)

    result = price_plan_change(
        subscription, ENTERPRISE, effective_at=PERIOD_START + timedelta(days=15)
    )

    assert result.unused_credit == Decimal("1495.00")  # 2990 / 2
    assert result.new_plan_charge == Decimal("4995.00")  # 9990 / 2


def test_currency_comes_from_the_target_plan() -> None:
    result = price_plan_change(_subscription(PROFESSIONAL), ENTERPRISE, effective_at=PERIOD_START)

    assert result.currency == "USD"


def test_effective_date_outside_the_period_is_rejected() -> None:
    """The rule's guard is reachable through the adapter, not bypassed by it."""
    subscription = _subscription(PROFESSIONAL)

    with pytest.raises(PricingError):
        price_plan_change(subscription, ENTERPRISE, effective_at=PERIOD_END + timedelta(days=1))


def test_period_boundaries_come_from_the_subscription_not_a_recomputation() -> None:
    """Stored boundaries are used verbatim, so billing and proration agree."""
    subscription = _subscription(PROFESSIONAL)
    subscription.current_period_end = PERIOD_START + timedelta(days=28)

    result = price_plan_change(
        subscription, ENTERPRISE, effective_at=PERIOD_START + timedelta(days=14)
    )

    assert result.days_in_period == 28
    assert result.days_remaining == 14
