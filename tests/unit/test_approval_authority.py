"""Approval authority, decidability and freshness (§13 layer 2).

These are the rules the approval API applies. Pure functions, so every
authorisation and replay path is verifiable with no database, no user and no
HTTP request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custops.domain.policies.approval_authority import (
    ApprovalAuthorityPolicy,
    ApproverRole,
    AuthorityCheck,
    AuthorityDenial,
    DecidabilityDenial,
    check_authority,
    check_decidable,
    is_stale,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
APPROVER = frozenset({ApproverRole.APPROVER})
FINANCE = frozenset({ApproverRole.APPROVER, ApproverRole.FINANCE_APPROVER})
VIEWER = frozenset({"viewer"})


def _authority(
    *,
    exists: bool = True,
    active: bool = True,
    roles: frozenset[str] = APPROVER,
    amount: Decimal | None = None,
    policy: ApprovalAuthorityPolicy | None = None,
) -> AuthorityCheck:
    return check_authority(
        actor_exists=exists,
        actor_is_active=active,
        actor_roles=roles,
        amount=amount,
        policy=policy,
    )


class TestAuthority:
    def test_an_approver_may_decide_a_routine_request(self) -> None:
        assert _authority().permitted

    def test_an_unknown_actor_is_refused(self) -> None:
        """Authority is never inferred from reaching the endpoint."""
        result = _authority(exists=False)

        assert not result.permitted
        assert result.denial == AuthorityDenial.ACTOR_NOT_FOUND

    def test_a_deactivated_actor_is_refused_despite_the_role(self) -> None:
        """A role alone must not confer authority on a closed account."""
        result = _authority(active=False)

        assert not result.permitted
        assert result.denial == AuthorityDenial.ACTOR_INACTIVE

    def test_an_actor_with_no_approving_role_is_refused(self) -> None:
        result = _authority(roles=VIEWER)

        assert not result.permitted
        assert result.denial == AuthorityDenial.NO_APPROVAL_ROLE

    def test_an_actor_with_no_roles_at_all_is_refused(self) -> None:
        result = _authority(roles=frozenset())

        assert not result.permitted
        assert result.denial == AuthorityDenial.NO_APPROVAL_ROLE

    def test_every_refusal_explains_itself(self) -> None:
        """A refused human should learn which of three things went wrong."""
        for result in (
            _authority(exists=False),
            _authority(active=False),
            _authority(roles=VIEWER),
            _authority(roles=APPROVER, amount=Decimal("50000.00")),
        ):
            assert not result.permitted
            assert result.denial
            assert result.message


class TestElevatedAuthority:
    def test_a_large_amount_requires_a_finance_approver(self) -> None:
        """Mirrors seeded policy DIS-002."""
        result = _authority(roles=APPROVER, amount=Decimal("25000.00"))

        assert not result.permitted
        assert result.denial == AuthorityDenial.ELEVATED_ROLE_REQUIRED

    def test_a_finance_approver_may_decide_a_large_amount(self) -> None:
        assert _authority(roles=FINANCE, amount=Decimal("25000.00")).permitted

    def test_an_amount_exactly_at_the_threshold_needs_no_elevation(self) -> None:
        """'Above threshold' means strictly greater, as elsewhere in the system."""
        assert _authority(roles=APPROVER, amount=Decimal("10000.00")).permitted

    def test_an_approval_with_no_amount_needs_no_elevation(self) -> None:
        assert _authority(roles=APPROVER, amount=None).permitted

    def test_an_admin_may_decide_a_large_amount(self) -> None:
        assert _authority(
            roles=frozenset({ApproverRole.ADMIN}), amount=Decimal("99999.00")
        ).permitted

    def test_the_threshold_is_configurable(self) -> None:
        strict = ApprovalAuthorityPolicy(elevated_amount_threshold=Decimal("100.00"))

        assert not _authority(roles=APPROVER, amount=Decimal("500.00"), policy=strict).permitted


class TestDecidability:
    def test_a_pending_approval_may_be_decided(self) -> None:
        assert check_decidable(status="pending", consumed_at=None).decidable

    def test_an_approved_request_cannot_be_re_decided(self) -> None:
        """Otherwise a rejection becomes an approval after the fact."""
        result = check_decidable(status="approved", consumed_at=None)

        assert not result.decidable
        assert result.denial == DecidabilityDenial.ALREADY_DECIDED

    def test_a_rejected_request_cannot_be_re_decided(self) -> None:
        result = check_decidable(status="rejected", consumed_at=None)

        assert not result.decidable
        assert result.denial == DecidabilityDenial.ALREADY_DECIDED

    def test_a_consumed_approval_cannot_be_decided(self) -> None:
        """One human decision authorises one action; a spent one is finished."""
        result = check_decidable(status="approved", consumed_at=NOW)

        assert not result.decidable
        assert result.denial == DecidabilityDenial.ALREADY_CONSUMED

    def test_consumption_outranks_status(self) -> None:
        """The more specific reason is the more useful one."""
        result = check_decidable(status="pending", consumed_at=NOW)

        assert result.denial == DecidabilityDenial.ALREADY_CONSUMED

    @pytest.mark.parametrize("state", ["expired", "cancelled", "something_new"])
    def test_any_other_status_is_not_decidable(self, state: str) -> None:
        """A status added later is refused by default, not accepted."""
        result = check_decidable(status=state, consumed_at=None)

        assert not result.decidable
        assert result.denial == DecidabilityDenial.NOT_PENDING


class TestFreshness:
    def test_a_recent_decision_is_current(self) -> None:
        assert not is_stale(decided_at=NOW - timedelta(hours=1), now=NOW)

    def test_an_old_decision_is_stale(self) -> None:
        """Approve today, execute next month is a replay in slow motion."""
        assert is_stale(decided_at=NOW - timedelta(days=30), now=NOW)

    def test_the_boundary_is_inclusive_of_the_window(self) -> None:
        assert not is_stale(decided_at=NOW - timedelta(hours=24), now=NOW)
        assert is_stale(decided_at=NOW - timedelta(hours=24, seconds=1), now=NOW)

    def test_an_approval_with_no_decision_time_is_stale(self) -> None:
        """It cannot show it is current, and 'unknown' reads as 'no'."""
        assert is_stale(decided_at=None, now=NOW)

    def test_the_window_is_configurable(self) -> None:
        short = ApprovalAuthorityPolicy(validity_window_hours=1)

        assert is_stale(decided_at=NOW - timedelta(hours=2), now=NOW, policy=short)
