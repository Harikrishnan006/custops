"""Which roles may reach which endpoint (§17).

A policy rather than a scattering of checks inside routers, for the same reason
the MCP permission matrix is data: "what can a viewer do?" should be answerable
by reading one table, not by tracing call sites.

Distinct from two neighbours it is easy to confuse with:

* ``mcp.permissions.matrix`` gates **agents** calling **tools**. This gates
  **humans and services** calling **HTTP endpoints**. An agent has no bearer
  token and a person has no tool role.
* ``domain.policies.approval_authority`` decides whether a given actor may
  approve a given *amount*. This only decides whether they may reach the
  endpoint at all. Both apply, in that order — reaching the endpoint is not
  authority to decide what it exposes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class EndpointAction(StrEnum):
    """What a caller is trying to do, named by intent rather than by route.

    Routes change; the question "may this caller start a workflow that mutates
    a customer's billing?" does not.
    """

    START_WORKFLOW = "start_workflow"
    READ_WORKFLOW = "read_workflow"
    LIST_APPROVALS = "list_approvals"
    READ_APPROVAL = "read_approval"
    DECIDE_APPROVAL = "decide_approval"


# Role names as seeded in `domain/seed.py`. Kept as plain strings because roles
# live in the database and an operator may add one without a code change.
ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_FINANCE_APPROVER = "finance_approver"
ROLE_VIEWER = "viewer"


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    """Who may perform one action."""

    action: str
    allowed_roles: frozenset[str]
    # Mutating actions change a customer's real state. Called out so the
    # distinction is visible in the table rather than inferred from the name.
    mutating: bool


def _policy(action: EndpointAction, *, roles: tuple[str, ...], mutating: bool) -> EndpointPolicy:
    return EndpointPolicy(action=action, allowed_roles=frozenset(roles), mutating=mutating)


# Reading is broad; acting is narrow. A viewer can inspect any trace — that is
# what makes the audit trail useful — but cannot start a workflow that moves a
# customer's money, and cannot decide an approval.
_READERS = (ROLE_OPERATOR, ROLE_APPROVER, ROLE_FINANCE_APPROVER, ROLE_VIEWER)

ENDPOINT_MATRIX: Mapping[str, EndpointPolicy] = MappingProxyType(
    {
        EndpointAction.READ_WORKFLOW: _policy(
            EndpointAction.READ_WORKFLOW, roles=_READERS, mutating=False
        ),
        EndpointAction.LIST_APPROVALS: _policy(
            EndpointAction.LIST_APPROVALS, roles=_READERS, mutating=False
        ),
        EndpointAction.READ_APPROVAL: _policy(
            EndpointAction.READ_APPROVAL, roles=_READERS, mutating=False
        ),
        # Starting a workflow reaches billing, CRM and the legacy portal. A
        # viewer holding this would make "read-only" untrue.
        EndpointAction.START_WORKFLOW: _policy(
            EndpointAction.START_WORKFLOW, roles=(ROLE_OPERATOR,), mutating=True
        ),
        # Reaching the endpoint is not authority over the amount — that is
        # `approval_authority`'s question, asked afterwards.
        EndpointAction.DECIDE_APPROVAL: _policy(
            EndpointAction.DECIDE_APPROVAL,
            roles=(ROLE_APPROVER, ROLE_FINANCE_APPROVER),
            mutating=True,
        ),
    }
)


class UnknownEndpointActionError(KeyError):
    """Raised for an action with no policy entry."""

    def __init__(self, action: str) -> None:
        super().__init__(f"No endpoint policy is defined for action '{action}'.")
        self.action = action


def get_policy(action: str) -> EndpointPolicy:
    """Look up a policy, failing closed on an unknown action.

    An endpoint added without a matrix entry is denied rather than defaulted, so
    the omission breaks loudly in tests instead of quietly granting whatever the
    default happened to be.
    """
    policy = ENDPOINT_MATRIX.get(action)
    if policy is None:
        raise UnknownEndpointActionError(action)
    return policy


def is_permitted(roles: frozenset[str], action: str) -> bool:
    return bool(get_policy(action).allowed_roles & roles)


def actions_for_roles(roles: frozenset[str]) -> tuple[str, ...]:
    return tuple(
        sorted(name for name, policy in ENDPOINT_MATRIX.items() if policy.allowed_roles & roles)
    )
