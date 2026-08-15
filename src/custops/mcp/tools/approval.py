"""Independent approval verification — decision D9.

**This is the boundary the whole human-in-the-loop design rests on.**

BUILD_SPEC §13 describes three layers: the graph routes to an approval gate, the
API records the human decision, and the mutating tool independently verifies the
record before acting. Only the third is load-bearing. The first two are a happy
path an LLM can route around — by calling the tool directly, by taking an edge
the planner invented, by retrying after an escalation. The tool cannot be routed
around, because it does the check itself, against the database, every time.

Concretely, verification requires **all** of:

* an approval row exists for this ``execution_id`` **and** this action,
* scoped to the exact entity being mutated,
* whose status is exactly ``APPROVED``,
* which has not already been consumed.

Each conjunct closes a specific hole. Without ``execution_id`` scoping, one
approval authorises every later workflow. Without the entity check, approval to
upgrade Acme authorises upgrading Globex. Without the exact-status test, a
``PENDING`` row reads as "not rejected". Without consumption, a retry loop
replays one human decision into many mutations.

The test that matters calls a mutating tool directly, with no graph involved,
and asserts rejection.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.domain.models.approval import Approval, ApprovalStatus
from custops.domain.policies.approval_authority import ApprovalAuthorityPolicy, is_stale
from custops.mcp.tools.results import ToolErrorCode, ToolExecutionError


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    """The exact act an approval must authorise."""

    execution_id: uuid.UUID
    action: str
    entity_type: str
    entity_id: str


async def verify_approval(
    session: AsyncSession,
    requirement: ApprovalRequirement,
    *,
    consume: bool = True,
    now: datetime | None = None,
    authority_policy: ApprovalAuthorityPolicy | None = None,
) -> Approval:
    """Return the approval authorising ``requirement``, or raise.

    ``consume`` marks the approval spent. Defaults to True because the common
    case is a single mutation per approval, and an unconsumed approval is a
    standing authorisation nobody intended to grant.
    """
    timestamp = now if now is not None else datetime.now(UTC)

    statement = select(Approval).where(
        Approval.execution_id == requirement.execution_id,
        Approval.action == requirement.action,
        Approval.entity_type == requirement.entity_type,
        Approval.entity_id == requirement.entity_id,
    )
    approval = (await session.execute(statement)).scalars().first()

    if approval is None:
        raise ToolExecutionError(
            ToolErrorCode.APPROVAL_REQUIRED,
            (
                f"Action '{requirement.action}' on {requirement.entity_type} "
                f"{requirement.entity_id} requires an approval record for execution "
                f"{requirement.execution_id}; none exists."
            ),
            action=requirement.action,
            execution_id=str(requirement.execution_id),
        )

    # Exact match on APPROVED. A not-REJECTED test would authorise PENDING, and
    # would silently authorise any status added later.
    if approval.status != ApprovalStatus.APPROVED:
        raise ToolExecutionError(
            ToolErrorCode.APPROVAL_NOT_GRANTED,
            (
                f"Approval {approval.id} for '{requirement.action}' has status "
                f"'{approval.status}', not 'approved'."
            ),
            approval_id=str(approval.id),
            status=approval.status,
        )

    if approval.consumed_at is not None:
        raise ToolExecutionError(
            ToolErrorCode.APPROVAL_ALREADY_CONSUMED,
            (
                f"Approval {approval.id} was already used at "
                f"{approval.consumed_at.isoformat()}; one human decision authorises "
                "one action."
            ),
            approval_id=str(approval.id),
        )

    # Freshness. An approval granted against one state of the world must not be
    # spendable weeks later against a different one — a replay in slow motion.
    # Checked here, in the tool layer, rather than trusting a sweeper job to
    # have flipped the status: layer 3's whole point is that it verifies for
    # itself (D9).
    if is_stale(decided_at=approval.decided_at, now=timestamp, policy=authority_policy):
        raise ToolExecutionError(
            ToolErrorCode.APPROVAL_NOT_GRANTED,
            (
                f"Approval {approval.id} was decided at "
                f"{approval.decided_at.isoformat() if approval.decided_at else 'an unknown time'} "
                "and is no longer current."
            ),
            approval_id=str(approval.id),
        )

    if consume:
        approval.consumed_at = timestamp

    return approval
