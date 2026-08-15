"""Proration and pricing arithmetic.

These are the calculations the Validator later recomputes and compares against
what the billing system stored, so "close enough" is a failure mode: a one-cent
divergence is indistinguishable from a real one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custops.domain.rules.pricing import (
    PricingError,
    ProrationInput,
    annualised_value,
    apply_discount,
    calculate_proration,
    to_money,
)

PERIOD_START = datetime(2026, 1, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 1, 31, tzinfo=UTC)  # 30 whole days


def _proration(**overrides: object) -> ProrationInput:
    defaults: dict[str, object] = {
        "current_unit_price": Decimal("100.00"),
        "new_unit_price": Decimal("300.00"),
        "seats": 1,
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "effective_at": PERIOD_START + timedelta(days=15),
    }
    defaults.update(overrides)
    return ProrationInput(**defaults)  # type: ignore[arg-type]


def test_half_period_upgrade_splits_both_sides() -> None:
    result = calculate_proration(_proration())

    assert result.days_in_period == 30
    assert result.days_remaining == 15
    assert result.unused_credit == Decimal("50.00")
    assert result.new_plan_charge == Decimal("150.00")
    assert result.amount_due == Decimal("100.00")
    assert result.currency == "USD"


def test_seats_scale_the_calculation() -> None:
    result = calculate_proration(_proration(seats=10))

    assert result.unused_credit == Decimal("500.00")
    assert result.new_plan_charge == Decimal("1500.00")
    assert result.amount_due == Decimal("1000.00")


def test_upgrade_on_the_first_day_charges_the_whole_period() -> None:
    result = calculate_proration(_proration(effective_at=PERIOD_START))

    assert result.days_remaining == 30
    assert result.unused_credit == Decimal("100.00")
    assert result.new_plan_charge == Decimal("300.00")
    assert result.amount_due == Decimal("200.00")


def test_upgrade_on_the_last_day_charges_nothing() -> None:
    result = calculate_proration(_proration(effective_at=PERIOD_END))

    assert result.days_remaining == 0
    assert result.unused_credit == Decimal("0.00")
    assert result.new_plan_charge == Decimal("0.00")
    assert result.amount_due == Decimal("0.00")


def test_downgrade_produces_a_credit_not_a_clamped_zero() -> None:
    """Money owed back to a customer must survive the calculation."""
    result = calculate_proration(
        _proration(current_unit_price=Decimal("300.00"), new_unit_price=Decimal("100.00"))
    )

    assert result.amount_due == Decimal("-100.00")


def test_rounding_is_half_up_not_bankers() -> None:
    """0.125 → 0.13. Decimal's default context would give 0.12."""
    result = calculate_proration(
        _proration(
            current_unit_price=Decimal("0.25"),
            new_unit_price=Decimal("0.25"),
            period_start=PERIOD_START,
            period_end=PERIOD_START + timedelta(days=2),
            effective_at=PERIOD_START + timedelta(days=1),
        )
    )

    assert result.unused_credit == Decimal("0.13")


def test_components_sum_to_the_total_shown_on_the_invoice() -> None:
    """Rounding each line then subtracting must equal the reported net."""
    result = calculate_proration(
        _proration(
            current_unit_price=Decimal("33.33"),
            new_unit_price=Decimal("99.99"),
            seats=7,
            effective_at=PERIOD_START + timedelta(days=11),
        )
    )

    assert result.new_plan_charge - result.unused_credit == result.amount_due


def test_breakdown_explains_the_figure() -> None:
    result = calculate_proration(_proration())

    assert result.breakdown["days_remaining"] == "15 of 30"
    assert "150.00" in result.breakdown["charge_for_remainder_at_new_plan"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"seats": 0}, "zero seats"),
        ({"current_unit_price": Decimal("-1.00")}, "negative current price"),
        ({"new_unit_price": Decimal("-1.00")}, "negative new price"),
        ({"period_end": PERIOD_START}, "empty period"),
        ({"effective_at": PERIOD_START - timedelta(days=1)}, "effective before period"),
        ({"effective_at": PERIOD_END + timedelta(days=1)}, "effective after period"),
    ],
)
def test_nonsensical_inputs_raise_rather_than_returning_a_plausible_number(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(PricingError):
        calculate_proration(_proration(**overrides))


def test_sub_day_period_is_rejected() -> None:
    with pytest.raises(PricingError):
        calculate_proration(
            _proration(
                period_start=PERIOD_START,
                period_end=PERIOD_START + timedelta(hours=6),
                effective_at=PERIOD_START,
            )
        )


class TestDiscount:
    def test_applies_a_percentage(self) -> None:
        assert apply_discount(Decimal("100.00"), Decimal("20")) == Decimal("80.00")

    def test_zero_percent_changes_nothing(self) -> None:
        assert apply_discount(Decimal("419.95"), Decimal("0")) == Decimal("419.95")

    def test_full_discount_is_free(self) -> None:
        assert apply_discount(Decimal("100.00"), Decimal("100")) == Decimal("0.00")

    def test_rounds_to_cents(self) -> None:
        assert apply_discount(Decimal("99.99"), Decimal("33.33")) == Decimal("66.66")

    @pytest.mark.parametrize("percent", [Decimal("-1"), Decimal("101")])
    def test_out_of_range_is_rejected(self, percent: Decimal) -> None:
        with pytest.raises(PricingError):
            apply_discount(Decimal("100.00"), percent)


class TestAnnualisedValue:
    def test_monthly_plan_over_twelve_periods(self) -> None:
        assert annualised_value(Decimal("300.00"), seats=5, periods_per_year=12) == Decimal(
            "18000.00"
        )

    @pytest.mark.parametrize(("seats", "periods"), [(0, 12), (5, 0)])
    def test_invalid_inputs_are_rejected(self, seats: int, periods: int) -> None:
        with pytest.raises(PricingError):
            annualised_value(Decimal("10.00"), seats=seats, periods_per_year=periods)


def test_to_money_always_yields_two_places() -> None:
    assert to_money(Decimal("1")) == Decimal("1.00")
    assert to_money(Decimal("1.005")) == Decimal("1.01")
    assert str(to_money(Decimal("2.5"))) == "2.50"
