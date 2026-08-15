"""Pricing and proration arithmetic.

All money is ``Decimal``. All rounding is explicit.

**Why ROUND_HALF_UP and not Python's default.** ``Decimal``'s default context
uses ROUND_HALF_EVEN (banker's rounding), which minimises statistical bias
across many roundings but is not what invoicing conventions or a customer's
expectation of "half a cent rounds up" describe. More importantly, the Validator
recomputes these figures and compares them to what the billing system stored: two
components rounding differently produces one-cent divergences that look exactly
like a real failure. The mode is therefore pinned here rather than inherited from
whatever context the caller happens to be running under.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")
HUNDRED = Decimal("100")


def to_money(value: Decimal) -> Decimal:
    """Quantise to two decimal places, rounding halves up."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


class PricingError(ValueError):
    """Raised when inputs cannot produce a meaningful price.

    A pricing rule that returns a plausible number for nonsensical input is more
    dangerous than one that refuses: the number flows into an invoice.
    """


@dataclass(frozen=True, slots=True)
class ProrationInput:
    """Everything needed to price a mid-cycle plan change."""

    current_unit_price: Decimal
    new_unit_price: Decimal
    seats: int
    period_start: datetime
    period_end: datetime
    effective_at: datetime
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class ProrationResult:
    """A priced plan change, with its workings exposed.

    ``breakdown`` exists so the figure can be explained to a human in an approval
    request and stored as structured evidence — not so that anything recomputes
    from it.
    """

    unused_credit: Decimal
    new_plan_charge: Decimal
    amount_due: Decimal
    days_remaining: int
    days_in_period: int
    currency: str
    breakdown: dict[str, str]


def calculate_proration(data: ProrationInput) -> ProrationResult:
    """Price a plan change taking effect part-way through a billing period.

    The customer is credited for the unused remainder of what they already paid
    and charged for the same remainder at the new rate::

        credit = current_price * seats * (days_remaining / days_in_period)
        charge = new_price     * seats * (days_remaining / days_in_period)
        due    = charge - credit

    A downgrade therefore yields a negative ``amount_due`` — a credit, not a
    charge. That is returned as-is rather than clamped to zero: silently
    discarding money owed to a customer is the kind of "helpful" behaviour that
    becomes a finance incident.
    """
    if data.seats < 1:
        raise PricingError(f"seats must be at least 1, got {data.seats}")
    if data.current_unit_price < 0 or data.new_unit_price < 0:
        raise PricingError("unit prices must not be negative")
    if data.period_end <= data.period_start:
        raise PricingError("period_end must be after period_start")
    if not (data.period_start <= data.effective_at <= data.period_end):
        raise PricingError(
            "effective_at must fall within the billing period "
            f"({data.period_start.isoformat()} … {data.period_end.isoformat()})"
        )

    days_in_period = (data.period_end - data.period_start).days
    if days_in_period <= 0:
        raise PricingError("billing period must span at least one whole day")

    days_remaining = (data.period_end - data.effective_at).days
    # Clamp defensively: a partial final day floors to 0, never to -1.
    days_remaining = max(0, min(days_remaining, days_in_period))

    remaining_fraction = Decimal(days_remaining) / Decimal(days_in_period)
    seats = Decimal(data.seats)

    unused_credit = to_money(data.current_unit_price * seats * remaining_fraction)
    new_plan_charge = to_money(data.new_unit_price * seats * remaining_fraction)
    # Subtract the already-rounded components rather than rounding the
    # difference: the invoice shows these two lines, and they must add up to the
    # total a customer can see.
    amount_due = to_money(new_plan_charge - unused_credit)

    return ProrationResult(
        unused_credit=unused_credit,
        new_plan_charge=new_plan_charge,
        amount_due=amount_due,
        days_remaining=days_remaining,
        days_in_period=days_in_period,
        currency=data.currency,
        breakdown={
            "days_remaining": f"{days_remaining} of {days_in_period}",
            "remaining_fraction": str(remaining_fraction.quantize(Decimal("0.0001"))),
            "credit_for_unused_current_plan": f"-{unused_credit} {data.currency}",
            "charge_for_remainder_at_new_plan": f"{new_plan_charge} {data.currency}",
            "net": f"{amount_due} {data.currency}",
        },
    )


def apply_discount(amount: Decimal, percent_off: Decimal) -> Decimal:
    """Reduce an amount by a percentage.

    Separate from proration on purpose: whether a discount *may* be applied is an
    approval question (see ``domain.policies.thresholds``), while what it comes
    to is arithmetic. Keeping them apart stops the authorisation check from being
    an incidental side effect of a calculation.
    """
    if percent_off < 0 or percent_off > HUNDRED:
        raise PricingError(f"percent_off must be between 0 and 100, got {percent_off}")
    return to_money(amount * (HUNDRED - percent_off) / HUNDRED)


def annualised_value(unit_price: Decimal, seats: int, periods_per_year: int) -> Decimal:
    """Contract value over a year, used for risk and approval thresholds."""
    if seats < 1:
        raise PricingError(f"seats must be at least 1, got {seats}")
    if periods_per_year < 1:
        raise PricingError(f"periods_per_year must be at least 1, got {periods_per_year}")
    return to_money(unit_price * Decimal(seats) * Decimal(periods_per_year))
