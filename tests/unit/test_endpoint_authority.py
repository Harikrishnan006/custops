"""Which roles may reach which endpoint (§17).

A policy, so it is testable without a request. The distinction that matters
most: **reading is broad, acting is narrow**. A viewer inspecting any trace is
what makes the audit trail useful; a viewer starting a workflow would move a
customer's money.
"""

from __future__ import annotations

import pytest

from custops.domain.policies.endpoint_authority import (
    ENDPOINT_MATRIX,
    ROLE_APPROVER,
    ROLE_FINANCE_APPROVER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    EndpointAction,
    UnknownEndpointActionError,
    actions_for_roles,
    get_policy,
    is_permitted,
)


def test_every_action_has_a_policy() -> None:
    """An endpoint added without an entry would be denied — loudly, here."""
    assert {str(action) for action in EndpointAction} == set(ENDPOINT_MATRIX)


def test_an_unknown_action_fails_closed() -> None:
    """Denied rather than defaulted: an omission must break in tests, not
    quietly grant whatever the default happened to be."""
    with pytest.raises(UnknownEndpointActionError):
        get_policy("delete_everything")


# --------------------------------------------------------------------- viewer


def test_a_viewer_may_read_a_trace() -> None:
    """The audit trail is only useful if it can be inspected."""
    assert is_permitted(frozenset({ROLE_VIEWER}), EndpointAction.READ_WORKFLOW)


def test_a_viewer_may_not_start_a_workflow() -> None:
    """Starting one reaches billing, CRM and the legacy portal. A viewer
    holding this would make "read-only" untrue."""
    assert not is_permitted(frozenset({ROLE_VIEWER}), EndpointAction.START_WORKFLOW)


def test_a_viewer_may_not_decide_an_approval() -> None:
    assert not is_permitted(frozenset({ROLE_VIEWER}), EndpointAction.DECIDE_APPROVAL)


def test_a_viewer_may_list_approvals_without_deciding_them() -> None:
    """Seeing what is pending is not authority over it."""
    roles = frozenset({ROLE_VIEWER})

    assert is_permitted(roles, EndpointAction.LIST_APPROVALS)
    assert not is_permitted(roles, EndpointAction.DECIDE_APPROVAL)


# ------------------------------------------------------------------- operator


def test_an_operator_may_start_a_workflow() -> None:
    assert is_permitted(frozenset({ROLE_OPERATOR}), EndpointAction.START_WORKFLOW)


def test_an_operator_alone_may_not_decide_an_approval() -> None:
    """Separation of duties: the person who starts the work does not get to
    approve it on the strength of having started it."""
    assert not is_permitted(frozenset({ROLE_OPERATOR}), EndpointAction.DECIDE_APPROVAL)


# ------------------------------------------------------------------ approvers


def test_an_approver_may_decide() -> None:
    assert is_permitted(frozenset({ROLE_APPROVER}), EndpointAction.DECIDE_APPROVAL)


def test_a_finance_approver_may_decide() -> None:
    assert is_permitted(frozenset({ROLE_FINANCE_APPROVER}), EndpointAction.DECIDE_APPROVAL)


def test_an_approver_may_not_start_a_workflow() -> None:
    assert not is_permitted(frozenset({ROLE_APPROVER}), EndpointAction.START_WORKFLOW)


def test_reaching_the_decision_endpoint_is_not_authority_over_the_amount() -> None:
    """Two checks apply in order, and this policy is only the first.

    ``approval_authority`` separately decides whether this actor may approve
    *this amount* — a plain approver reaching the endpoint does not mean they
    can sign off a six-figure upgrade.
    """
    from custops.domain.policies.approval_authority import check_authority

    roles = frozenset({ROLE_APPROVER})
    assert is_permitted(roles, EndpointAction.DECIDE_APPROVAL)

    from decimal import Decimal

    verdict = check_authority(
        actor_exists=True,
        actor_is_active=True,
        actor_roles=roles,
        amount=Decimal("100000.00"),
        policy=None,
    )
    assert not verdict.permitted


# --------------------------------------------------------------------- shapes


def test_no_role_grants_everything() -> None:
    """If one role could do everything, the matrix would be decoration."""
    for role in (ROLE_OPERATOR, ROLE_APPROVER, ROLE_FINANCE_APPROVER, ROLE_VIEWER):
        granted = set(actions_for_roles(frozenset({role})))
        assert granted != {str(action) for action in EndpointAction}, role


def test_holding_no_role_grants_nothing() -> None:
    """An authenticated caller with no roles can reach nothing."""
    assert actions_for_roles(frozenset()) == ()


def test_mutating_actions_are_marked_as_such() -> None:
    """The distinction should be readable from the table, not inferred from
    the action's name."""
    assert get_policy(EndpointAction.START_WORKFLOW).mutating
    assert get_policy(EndpointAction.DECIDE_APPROVAL).mutating
    assert not get_policy(EndpointAction.READ_WORKFLOW).mutating


def test_every_mutating_action_is_narrower_than_reading() -> None:
    """The shape of the whole policy, asserted rather than left to inspection."""
    readers = set(get_policy(EndpointAction.READ_WORKFLOW).allowed_roles)

    for action, policy in ENDPOINT_MATRIX.items():
        if policy.mutating:
            assert set(policy.allowed_roles) < readers, action
