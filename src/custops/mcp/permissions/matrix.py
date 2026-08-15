"""Which caller may invoke which tool, and what that tool may do.

Two independent gates, deliberately not collapsed into one:

* **Permission** — may this caller invoke this tool at all? A Research agent has
  no business calling ``update_subscription`` regardless of any approval.
* **Approval** — for a mutating tool, has a human authorised *this specific
  action* on *this specific entity* in *this execution*? (decision D9, enforced
  in ``mcp.tools.approval``.)

Permission is about capability; approval is about a particular act. Merging them
would mean an agent that holds a mutating permission could act without a human,
or that an approval could substitute for never having been granted the
capability. Both are wrong.

The matrix is data, not code branches, so "what can the Execution agent do?" is
answered by reading one table rather than by tracing call sites.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    """Callers of the tool layer.

    Agent roles, not human roles: these gate what an *agent* may do. Human
    authorisation is the approval record.
    """

    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    RESEARCH = "research"
    EXECUTION = "execution"
    VALIDATOR = "validator"
    BILLING_SPECIALIST = "billing_specialist"
    # Operators and tests. Never granted to an agent.
    ADMIN = "admin"


class ToolName(StrEnum):
    """Every tool the platform exposes (BUILD_SPEC §8)."""

    GET_CUSTOMER = "get_customer"
    GET_SUBSCRIPTION = "get_subscription"
    UPDATE_SUBSCRIPTION = "update_subscription"
    GET_CONTRACT = "get_contract"
    GET_PRICING = "get_pricing"
    GET_INVOICE = "get_invoice"
    CREATE_REFUND = "create_refund"
    GET_SUPPORT_HISTORY = "get_support_history"
    UPDATE_CRM = "update_crm"
    SEARCH_KNOWLEDGE = "search_knowledge"
    SEND_NOTIFICATION = "send_notification"


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """What a tool is, for permission purposes."""

    tool: str
    # Mutating tools change state in a system of record. They require an
    # approval record; read tools never do.
    mutating: bool
    allowed_roles: frozenset[str]
    # The action name an approval must carry to authorise this tool. None for
    # read tools.
    approval_action: str | None = None


def _policy(
    tool: ToolName,
    *,
    mutating: bool,
    roles: tuple[Role, ...],
    approval_action: str | None = None,
) -> ToolPolicy:
    return ToolPolicy(
        tool=tool,
        mutating=mutating,
        allowed_roles=frozenset(roles),
        approval_action=approval_action,
    )


# Read tools are broadly available: gathering evidence is what Research and
# Validator exist to do, and restricting reads would push agents toward
# guessing. Mutating tools are narrow — Execution only.
_READERS = (Role.SUPERVISOR, Role.RESEARCH, Role.EXECUTION, Role.VALIDATOR, Role.ADMIN)

PERMISSION_MATRIX: Mapping[str, ToolPolicy] = MappingProxyType(
    {
        ToolName.GET_CUSTOMER: _policy(ToolName.GET_CUSTOMER, mutating=False, roles=_READERS),
        ToolName.GET_SUBSCRIPTION: _policy(
            ToolName.GET_SUBSCRIPTION,
            mutating=False,
            roles=(*_READERS, Role.BILLING_SPECIALIST),
        ),
        ToolName.GET_CONTRACT: _policy(
            ToolName.GET_CONTRACT, mutating=False, roles=(*_READERS, Role.BILLING_SPECIALIST)
        ),
        ToolName.GET_PRICING: _policy(
            ToolName.GET_PRICING, mutating=False, roles=(*_READERS, Role.BILLING_SPECIALIST)
        ),
        ToolName.GET_INVOICE: _policy(
            ToolName.GET_INVOICE, mutating=False, roles=(*_READERS, Role.BILLING_SPECIALIST)
        ),
        ToolName.GET_SUPPORT_HISTORY: _policy(
            ToolName.GET_SUPPORT_HISTORY, mutating=False, roles=_READERS
        ),
        ToolName.SEARCH_KNOWLEDGE: _policy(
            ToolName.SEARCH_KNOWLEDGE, mutating=False, roles=(*_READERS, Role.PLANNER)
        ),
        # --- Mutating: Execution only, and each needs an approval record. ---
        ToolName.UPDATE_SUBSCRIPTION: _policy(
            ToolName.UPDATE_SUBSCRIPTION,
            mutating=True,
            roles=(Role.EXECUTION, Role.ADMIN),
            approval_action="subscription_upgrade",
        ),
        ToolName.UPDATE_CRM: _policy(
            ToolName.UPDATE_CRM,
            mutating=True,
            roles=(Role.EXECUTION, Role.ADMIN),
            approval_action="update_crm",
        ),
        ToolName.CREATE_REFUND: _policy(
            ToolName.CREATE_REFUND,
            mutating=True,
            roles=(Role.EXECUTION, Role.ADMIN),
            approval_action="issue_refund",
        ),
        ToolName.SEND_NOTIFICATION: _policy(
            ToolName.SEND_NOTIFICATION,
            mutating=True,
            roles=(Role.EXECUTION, Role.ADMIN),
            approval_action="send_notification",
        ),
    }
)


class PermissionDeniedError(PermissionError):
    """Raised when a caller may not invoke a tool."""

    def __init__(self, role: str, tool: str) -> None:
        super().__init__(f"Role '{role}' is not permitted to call tool '{tool}'.")
        self.role = role
        self.tool = tool


class UnknownToolError(KeyError):
    """Raised for a tool with no policy entry."""

    def __init__(self, tool: str) -> None:
        super().__init__(f"No permission policy is defined for tool '{tool}'.")
        self.tool = tool


def get_policy(tool: str) -> ToolPolicy:
    """Look up a tool's policy, failing closed on an unknown tool.

    An undeclared tool is denied rather than defaulted: a tool added without a
    matrix entry should break loudly in tests, not quietly acquire whatever the
    default happens to be.
    """
    policy = PERMISSION_MATRIX.get(tool)
    if policy is None:
        raise UnknownToolError(tool)
    return policy


def check_permission(role: str, tool: str) -> ToolPolicy:
    """Authorise a caller for a tool, or raise."""
    policy = get_policy(tool)
    if role not in policy.allowed_roles:
        raise PermissionDeniedError(role, tool)
    return policy


def is_mutating(tool: str) -> bool:
    return get_policy(tool).mutating


def tools_for_role(role: str) -> tuple[str, ...]:
    """Every tool a role may call. Used to scope an agent's tool list."""
    return tuple(
        sorted(name for name, policy in PERMISSION_MATRIX.items() if role in policy.allowed_roles)
    )
