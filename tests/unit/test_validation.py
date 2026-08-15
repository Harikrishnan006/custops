"""Cross-system validation (BUILD_SPEC §14).

The property under test: a change that billing accepted is not an outcome until
every system agrees. These are pure comparisons, so the whole rule is verifiable
without any of the systems existing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from custops.agents.state import ValidationResult, ValidationVerdict
from custops.agents.validation import (
    ExpectedState,
    ObservedState,
    divergent_systems,
    overall_verdict,
    validate_upgrade,
)

EXPECTED = ExpectedState(plan_code="enterprise")


def _observed(**overrides: Any) -> ObservedState:
    defaults: dict[str, Any] = {
        "billing_plan_code": "enterprise",
        "billing_status": "active",
        "crm_plan_code": "enterprise",
        "entitlement_tier": "enterprise",
    }
    defaults.update(overrides)
    return ObservedState(**defaults)


def _verdict_for(results: list[ValidationResult], check: str) -> str:
    return next(r["verdict"] for r in results if r["check"] == check)


class TestAllSystemsAgree:
    def test_every_check_passes(self) -> None:
        results = validate_upgrade(EXPECTED, _observed())

        assert overall_verdict(results) == ValidationVerdict.PASS
        assert divergent_systems(results) == ()

    def test_all_four_systems_are_checked(self) -> None:
        """§14 names billing plan, billing status, CRM, and entitlement."""
        results = validate_upgrade(EXPECTED, _observed())

        assert {r["check"] for r in results} == {
            "subscription_plan",
            "subscription_status",
            "crm_plan_reference",
            "entitlement_tier",
        }


class TestTheDivergenceThatMatters:
    def test_billing_succeeded_but_entitlement_never_flipped(self) -> None:
        """The failure D8 exists to catch: billed for Enterprise, running Professional."""
        results = validate_upgrade(EXPECTED, _observed(entitlement_tier="professional"))

        assert overall_verdict(results) == ValidationVerdict.FAIL
        assert divergent_systems(results) == ("provisioning",)
        assert _verdict_for(results, "subscription_plan") == ValidationVerdict.PASS
        assert _verdict_for(results, "entitlement_tier") == ValidationVerdict.FAIL

    def test_two_of_three_agreeing_is_still_a_failure(self) -> None:
        """A majority is not a consensus; §14 requires all."""
        results = validate_upgrade(EXPECTED, _observed(crm_plan_code="professional"))

        assert overall_verdict(results) == ValidationVerdict.FAIL

    def test_a_plan_change_that_left_the_subscription_inactive_fails(self) -> None:
        results = validate_upgrade(EXPECTED, _observed(billing_status="past_due"))

        assert overall_verdict(results) == ValidationVerdict.FAIL
        assert _verdict_for(results, "subscription_status") == ValidationVerdict.FAIL

    def test_every_diverging_system_is_named(self) -> None:
        results = validate_upgrade(
            EXPECTED, _observed(crm_plan_code="professional", entitlement_tier="starter")
        )

        assert divergent_systems(results) == ("crm", "provisioning")


class TestUnreadableSystems:
    def test_an_unreadable_system_needs_review_rather_than_passing(self) -> None:
        """Silently dropping a check would let an unverified outcome look verified."""
        results = validate_upgrade(EXPECTED, _observed(billing_plan_code=None))

        assert overall_verdict(results) == ValidationVerdict.NEEDS_REVIEW
        assert _verdict_for(results, "subscription_plan") == ValidationVerdict.NEEDS_REVIEW

    def test_an_unconsulted_provisioning_system_needs_review(self) -> None:
        """'Not consulted' is different from 'disagrees', and reported so."""
        results = validate_upgrade(EXPECTED, _observed(entitlement_tier=None))

        assert _verdict_for(results, "entitlement_tier") == ValidationVerdict.NEEDS_REVIEW
        assert overall_verdict(results) == ValidationVerdict.NEEDS_REVIEW

    def test_a_real_failure_outranks_an_unreadable_system(self) -> None:
        """FAIL beats NEEDS_REVIEW: a known divergence is the more serious signal."""
        results = validate_upgrade(
            EXPECTED, _observed(billing_plan_code=None, crm_plan_code="professional")
        )

        assert overall_verdict(results) == ValidationVerdict.FAIL


class TestMoney:
    def test_a_matching_proration_passes(self) -> None:
        results = validate_upgrade(
            ExpectedState(plan_code="enterprise", proration_amount=Decimal("466.67")),
            _observed(invoice_amount_due=Decimal("466.67")),
        )

        assert _verdict_for(results, "proration_amount") == ValidationVerdict.PASS

    def test_a_one_cent_difference_fails(self) -> None:
        """Money is compared exactly; 'close' is not a financial outcome."""
        results = validate_upgrade(
            ExpectedState(plan_code="enterprise", proration_amount=Decimal("466.67")),
            _observed(invoice_amount_due=Decimal("466.68")),
        )

        assert _verdict_for(results, "proration_amount") == ValidationVerdict.FAIL
        assert overall_verdict(results) == ValidationVerdict.FAIL

    def test_the_check_is_skipped_when_no_amount_was_expected(self) -> None:
        results = validate_upgrade(EXPECTED, _observed())

        assert not any(r["check"] == "proration_amount" for r in results)


class TestOverallVerdict:
    def test_validating_nothing_is_not_success(self) -> None:
        assert overall_verdict([]) == ValidationVerdict.NEEDS_REVIEW

    def test_results_are_reproducible(self) -> None:
        """The Validator re-runs this; identical inputs must give identical output."""
        observed = _observed(entitlement_tier="professional")

        assert validate_upgrade(EXPECTED, observed) == validate_upgrade(EXPECTED, observed)

    def test_every_result_records_expected_and_actual(self) -> None:
        """A human reading the trace needs both sides of the comparison."""
        results = validate_upgrade(EXPECTED, _observed(entitlement_tier="professional"))

        for result in results:
            assert result["expected"]
            assert result["actual"]
