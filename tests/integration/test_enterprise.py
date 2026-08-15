"""Enterprise services and upgrade assessment against a real database.

These are the acceptance tests for Phase 2. Each seeded scenario exists to drive
one branch of the eligibility and approval rules, so this file reads as a
catalogue of the paths the Subscription Upgrade workflow must survive — the happy
one and, more importantly, the six that are not.

Requires PostgreSQL with migrations applied:

    uv run alembic upgrade head
    uv run custops seed
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from custops.apps.enterprise import assessment as assessment_module
from custops.apps.enterprise.billing import service as billing_service
from custops.apps.enterprise.contracts import service as contract_service
from custops.apps.enterprise.crm import service as crm_service
from custops.apps.enterprise.support import service as support_service
from custops.db.engine import Database
from custops.domain.enums import UpgradeRestriction
from custops.domain.policies.thresholds import ApprovalTrigger
from custops.domain.rules.eligibility import ApprovalCode, BlockerCode, WarningCode
from custops.domain.seed import seed_all, seed_id
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """A seeded session, rolled back afterwards.

    Everything happens in one transaction that is never committed, so the tests
    leave the database exactly as they found it and can run in any order.
    """
    async with database.session_factory() as db_session:
        await seed_all(db_session, now=NOW)
        try:
            yield db_session
        finally:
            await db_session.rollback()


def account_id(key: str) -> uuid.UUID:
    return seed_id("account", key)


async def assess(
    session: AsyncSession, key: str, target: str = "enterprise"
) -> assessment_module.UpgradeAssessment:
    return await assessment_module.assess_upgrade(
        session, account_id=account_id(key), target_plan_code=target, now=NOW
    )


def codes(findings: tuple[object, ...]) -> set[str]:
    return {finding.code for finding in findings}  # type: ignore[attr-defined]


class TestSeed:
    async def test_seeding_is_idempotent(self, session: AsyncSession) -> None:
        """Re-running the seed must update, not duplicate."""
        first = await crm_service.get_customer_by_ref(session, "ACME")
        await seed_all(session, now=NOW)
        second = await crm_service.get_customer_by_ref(session, "ACME")

        assert first is not None
        assert second is not None
        assert first.id == second.id

    async def test_plan_catalogue_is_ranked(self, session: AsyncSession) -> None:
        plans = await billing_service.list_plans(session, active_only=True)

        assert [plan.code for plan in plans] == ["starter", "professional", "enterprise"]
        assert [plan.rank for plan in plans] == [1, 2, 3]

    async def test_retired_plan_is_excluded_from_the_active_catalogue(
        self, session: AsyncSession
    ) -> None:
        active = await billing_service.list_plans(session, active_only=True)
        everything = await billing_service.list_plans(session, active_only=False)

        assert "professional_legacy" not in {plan.code for plan in active}
        assert "professional_legacy" in {plan.code for plan in everything}


class TestCrmService:
    async def test_customer_lookup_is_case_insensitive(self, session: AsyncSession) -> None:
        """A request says "upgrade Acme", not "upgrade ACME"."""
        assert await crm_service.get_customer_by_ref(session, "acme") is not None
        assert await crm_service.get_customer_by_ref(session, "AcMe") is not None

    async def test_unknown_customer_returns_none(self, session: AsyncSession) -> None:
        assert await crm_service.get_customer_by_ref(session, "NOSUCHCO") is None

    async def test_name_search_returns_candidates_not_a_guess(self, session: AsyncSession) -> None:
        matches = await crm_service.search_customers_by_name(session, "o")

        assert len(matches) > 1

    async def test_primary_contact_is_resolvable(self, session: AsyncSession) -> None:
        contact = await crm_service.get_primary_contact(session, account_id("acme"))

        assert contact is not None
        assert contact.is_primary
        assert contact.email == "ops@acme.example.com"

    async def test_crm_caches_its_own_copy_of_the_plan(self, session: AsyncSession) -> None:
        """The field the Validator cross-checks against billing and the portal."""
        account = await crm_service.get_account(session, account_id("acme"))

        assert account is not None
        assert account.current_plan_code == "professional"


class TestBillingService:
    async def test_active_subscription_is_found(self, session: AsyncSession) -> None:
        subscription = await billing_service.get_active_subscription(session, account_id("acme"))

        assert subscription is not None
        assert subscription.plan.code == "professional"
        assert subscription.seats == 20

    async def test_past_due_invoices_are_counted_from_the_due_date(
        self, session: AsyncSession
    ) -> None:
        """Computed against the clock, not a stored status that may be stale."""
        assert (
            await billing_service.count_past_due_invoices(session, account_id("initech"), now=NOW)
            == 1
        )
        assert (
            await billing_service.count_past_due_invoices(session, account_id("acme"), now=NOW) == 0
        )

    async def test_active_discount_is_found(self, session: AsyncSession) -> None:
        discount = await billing_service.get_active_discount(
            session, account_id("umbrella"), now=NOW
        )

        assert discount is not None
        # Exact Decimal comparison, not approx: mixing Decimal with float
        # tolerance is the habit this codebase avoids everywhere money is
        # involved.
        assert discount.percent_off == Decimal("35.00")


class TestContractService:
    async def test_active_contract_requires_status_and_dates_to_agree(
        self, session: AsyncSession
    ) -> None:
        contract = await contract_service.get_active_contract(
            session, account_id("globex"), now=NOW
        )

        assert contract is not None
        assert contract.upgrade_restriction == UpgradeRestriction.TERM_LOCKED

    async def test_account_without_a_contract_returns_none(self, session: AsyncSession) -> None:
        assert (
            await contract_service.get_active_contract(session, account_id("initech"), now=NOW)
            is None
        )

    async def test_policies_are_retrievable_by_code(self, session: AsyncSession) -> None:
        policy = await contract_service.get_policy(session, "UPG-001", now=NOW)

        assert policy is not None
        assert "eligible for a subscription upgrade" in policy.body


class TestSupportService:
    async def test_urgent_open_tickets_are_counted(self, session: AsyncSession) -> None:
        assert await support_service.count_open_urgent_tickets(session, account_id("hooli")) == 2

    async def test_summary_aggregates_in_one_round_trip(self, session: AsyncSession) -> None:
        summary = await support_service.summarise_support(session, account_id("hooli"))

        assert summary.total_tickets == 5
        assert summary.unresolved_tickets == 3
        assert summary.open_urgent_tickets == 2
        assert summary.average_satisfaction is not None


class TestUpgradeAssessment:
    async def test_healthy_account_proceeds_automatically(self, session: AsyncSession) -> None:
        result = await assess(session, "acme")

        assert result.eligibility.eligible
        assert not result.approval.required
        assert result.can_proceed_automatically
        assert result.current_plan_code == "professional"
        assert result.target_plan_code == "enterprise"

    async def test_proration_is_computed_and_explained(self, session: AsyncSession) -> None:
        result = await assess(session, "acme")

        # 20 seats, professional -> enterprise, 20 of 30 days remaining.
        assert result.proration.days_in_period == 30
        assert result.proration.amount_due > 0
        assert result.proration.breakdown["days_remaining"].endswith("of 30")

    async def test_term_locked_contract_blocks(self, session: AsyncSession) -> None:
        result = await assess(session, "globex")

        assert not result.eligibility.eligible
        assert BlockerCode.CONTRACT_TERM_LOCKED in codes(result.eligibility.blockers)
        assert not result.can_proceed_automatically

    async def test_suspended_account_with_arrears_reports_both_blockers(
        self, session: AsyncSession
    ) -> None:
        result = await assess(session, "initech")

        assert codes(result.eligibility.blockers) >= {
            BlockerCode.ACCOUNT_NOT_ACTIVE,
            BlockerCode.OUTSTANDING_PAST_DUE_INVOICES,
        }

    async def test_deep_discount_requires_approval_but_is_still_eligible(
        self, session: AsyncSession
    ) -> None:
        result = await assess(session, "umbrella")

        assert result.eligibility.eligible
        assert result.approval.required
        assert ApprovalTrigger.DISCOUNT_ABOVE_THRESHOLD in result.approval.triggers
        assert not result.can_proceed_automatically

    async def test_ambiguous_contract_escalates_to_a_human(self, session: AsyncSession) -> None:
        result = await assess(session, "vehement")

        assert result.eligibility.eligible
        assert ApprovalCode.CONTRACT_TERMS_AMBIGUOUS in codes(result.eligibility.approvals_required)
        assert ApprovalTrigger.CONTRACT_TERMS_AMBIGUOUS in result.approval.triggers

    async def test_open_urgent_tickets_warn_without_blocking(self, session: AsyncSession) -> None:
        result = await assess(session, "hooli")

        assert result.eligibility.eligible
        assert WarningCode.OPEN_URGENT_TICKETS in codes(result.eligibility.warnings)

    async def test_inactive_customer_blocks(self, session: AsyncSession) -> None:
        result = await assess(session, "soylent")

        assert BlockerCode.CUSTOMER_NOT_ACTIVE in codes(result.eligibility.blockers)

    async def test_downgrade_is_not_an_upgrade(self, session: AsyncSession) -> None:
        result = await assess(session, "acme", target="starter")

        assert BlockerCode.NOT_AN_UPGRADE in codes(result.eligibility.blockers)

    async def test_retired_target_plan_blocks(self, session: AsyncSession) -> None:
        result = await assess(session, "initech", target="professional_legacy")

        assert BlockerCode.TARGET_PLAN_INACTIVE in codes(result.eligibility.blockers)

    async def test_evidence_cites_sources_for_every_fact(self, session: AsyncSession) -> None:
        result = await assess(session, "globex")

        assert result.evidence["contract"] == "contract:CTR-GLOBEX-001"
        assert result.evidence["account"].startswith("account:")
        assert "past_due_invoice_count" in result.evidence


class TestAssessmentFailures:
    """'The question could not be asked' is distinct from 'the answer is no'."""

    async def test_unknown_account_raises(self, session: AsyncSession) -> None:
        with pytest.raises(assessment_module.AssessmentError) as error:
            await assessment_module.assess_upgrade(
                session,
                account_id=uuid.uuid4(),
                target_plan_code="enterprise",
                now=NOW,
            )

        assert error.value.code == assessment_module.AssessmentErrorCode.ACCOUNT_NOT_FOUND

    async def test_unknown_plan_raises(self, session: AsyncSession) -> None:
        with pytest.raises(assessment_module.AssessmentError) as error:
            await assess(session, "acme", target="platinum_deluxe")

        assert error.value.code == assessment_module.AssessmentErrorCode.TARGET_PLAN_NOT_FOUND
