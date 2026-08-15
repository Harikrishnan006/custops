"""Cross-system validation (BUILD_SPEC §14).

The premise: **a 200 response is not a business outcome.** The billing API can
accept a plan change and return success while the entitlement was never
provisioned, leaving a customer billed for Enterprise and running Professional.
So validation never reads the executing agent's return value — it re-reads each
system of record and compares.

Everything here is a pure function over values already read. The *reading* is
the node's job; the *comparing* is this module's, which is what makes the
comparison logic testable without any of the systems being present.

``PASS`` requires every check to pass. A single ``FAIL`` fails the set, and any
``NEEDS_REVIEW`` downgrades the set to needing review — "I could not tell"
never rounds up to "fine".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from custops.agents.state import ValidationResult, ValidationVerdict


@dataclass(frozen=True, slots=True)
class ObservedState:
    """What each system of record actually says, read back after execution.

    ``entitlement_tier`` is ``None`` when the provisioning system has not been
    consulted at all — which is different from it disagreeing, and is reported
    differently.
    """

    billing_plan_code: str | None
    billing_status: str | None
    crm_plan_code: str | None
    entitlement_tier: str | None
    invoice_amount_due: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExpectedState:
    """What the workflow intended to bring about."""

    plan_code: str
    subscription_status: str = "active"
    proration_amount: Decimal | None = None


def _result(check: str, system: str, verdict: str, expected: str, actual: str) -> ValidationResult:
    return ValidationResult(
        check=check, system=system, verdict=verdict, expected=expected, actual=actual
    )


def validate_upgrade(expected: ExpectedState, observed: ObservedState) -> list[ValidationResult]:
    """Compare intent against what each system now reports.

    Returns one result per system consulted. A system that could not be read
    produces ``NEEDS_REVIEW`` rather than being omitted — silently dropping a
    check would let an unverified outcome look fully verified.
    """
    results: list[ValidationResult] = []

    # --- Billing: the plan it will charge for --------------------------------
    if observed.billing_plan_code is None:
        results.append(
            _result(
                "subscription_plan",
                "billing",
                ValidationVerdict.NEEDS_REVIEW,
                expected.plan_code,
                "unreadable",
            )
        )
    else:
        results.append(
            _result(
                "subscription_plan",
                "billing",
                ValidationVerdict.PASS
                if observed.billing_plan_code == expected.plan_code
                else ValidationVerdict.FAIL,
                expected.plan_code,
                observed.billing_plan_code,
            )
        )

    # A plan change that left the subscription inactive is not a success.
    if observed.billing_status is not None:
        results.append(
            _result(
                "subscription_status",
                "billing",
                ValidationVerdict.PASS
                if observed.billing_status == expected.subscription_status
                else ValidationVerdict.FAIL,
                expected.subscription_status,
                observed.billing_status,
            )
        )

    # --- CRM: its own cached copy of the plan --------------------------------
    if observed.crm_plan_code is None:
        results.append(
            _result(
                "crm_plan_reference",
                "crm",
                ValidationVerdict.NEEDS_REVIEW,
                expected.plan_code,
                "unreadable",
            )
        )
    else:
        results.append(
            _result(
                "crm_plan_reference",
                "crm",
                ValidationVerdict.PASS
                if observed.crm_plan_code == expected.plan_code
                else ValidationVerdict.FAIL,
                expected.plan_code,
                observed.crm_plan_code,
            )
        )

    # --- Provisioning: what the customer can actually use --------------------
    #
    # This is the check the whole architecture exists for (D8). Until Phase 8
    # drives the legacy portal, nothing flips the entitlement, so an otherwise
    # successful upgrade is *expected* to fail here — and should. Reporting PASS
    # because billing agreed with the CRM would be precisely the false
    # confidence §14 is written to prevent.
    if observed.entitlement_tier is None:
        results.append(
            _result(
                "entitlement_tier",
                "provisioning",
                ValidationVerdict.NEEDS_REVIEW,
                expected.plan_code,
                "not consulted",
            )
        )
    else:
        results.append(
            _result(
                "entitlement_tier",
                "provisioning",
                ValidationVerdict.PASS
                if observed.entitlement_tier == expected.plan_code
                else ValidationVerdict.FAIL,
                expected.plan_code,
                observed.entitlement_tier,
            )
        )

    # --- Money: recomputed, not trusted --------------------------------------
    if expected.proration_amount is not None and observed.invoice_amount_due is not None:
        results.append(
            _result(
                "proration_amount",
                "billing",
                ValidationVerdict.PASS
                if observed.invoice_amount_due == expected.proration_amount
                else ValidationVerdict.FAIL,
                str(expected.proration_amount),
                str(observed.invoice_amount_due),
            )
        )

    return results


def overall_verdict(results: list[ValidationResult]) -> str:
    """Collapse a set of checks into one verdict.

    Order of precedence is deliberate: ``FAIL`` beats ``NEEDS_REVIEW`` beats
    ``PASS``. An empty set is ``NEEDS_REVIEW``, not ``PASS`` — validating
    nothing is not the same as validating successfully.
    """
    if not results:
        return ValidationVerdict.NEEDS_REVIEW

    verdicts = {result["verdict"] for result in results}
    if ValidationVerdict.FAIL in verdicts:
        return ValidationVerdict.FAIL
    if ValidationVerdict.NEEDS_REVIEW in verdicts:
        return ValidationVerdict.NEEDS_REVIEW
    return ValidationVerdict.PASS


def divergent_systems(results: list[ValidationResult]) -> tuple[str, ...]:
    """Which systems disagreed, for the escalation message a human reads."""
    return tuple(sorted({r["system"] for r in results if r["verdict"] == ValidationVerdict.FAIL}))
