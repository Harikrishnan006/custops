"""Approval endpoints — layer 2 of §13's three-layer enforcement.

**This layer records the human decision with actor and timestamp.** It does not
enforce the mutation: layer 3 (the MCP tool) verifies the record independently
before acting, and would refuse even if this endpoint were bypassed entirely.
That redundancy is the design, not duplication.

Ordering inside `decide` is deliberate: authority, then decidability, then
write, then resume. The decision is committed **before** the workflow is
resumed, so a crash between the two leaves a recorded decision and a resumable
workflow rather than a workflow that acted on a decision nobody recorded.

**The actor comes from authentication** (Phase 13). It is the bearer token's
principal, never a field in the request body — a caller can no longer claim to
be the finance approver. Two checks apply in order: reaching this endpoint
requires an approving role (``endpoint_authority``), and deciding *this* amount
requires sufficient authority (``approval_authority``). The audit trail records
the authenticated principal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from custops.apps.api.routers.workflows import get_runner
from custops.apps.api.schemas.approval import (
    ApprovalDecisionOut,
    ApprovalDecisionRequest,
    ApprovalOut,
)
from custops.apps.api.security.principal import (
    DecideApprovalPrincipal,
    ListApprovalsPrincipal,
)
from custops.apps.enterprise.router import get_session
from custops.apps.orchestrator.runner import WorkflowRunner
from custops.domain.models.approval import Approval, ApprovalStatus
from custops.domain.models.identity import User
from custops.domain.policies.approval_authority import (
    ApprovalAuthorityPolicy,
    check_authority,
    check_decidable,
)
from custops.observability.audit import record_event
from custops.observability.events import ActorType, EventType
from custops.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RunnerDep = Annotated[WorkflowRunner, Depends(get_runner)]


def get_authority_policy() -> ApprovalAuthorityPolicy:
    """Overridable so a deployment or a test can tighten authority."""
    return ApprovalAuthorityPolicy()


PolicyDep = Annotated[ApprovalAuthorityPolicy, Depends(get_authority_policy)]


def _to_out(approval: Approval) -> ApprovalOut:
    return ApprovalOut(
        id=approval.id,
        execution_id=approval.execution_id,
        action=approval.action,
        entity_type=approval.entity_type,
        entity_id=approval.entity_id,
        status=approval.status,
        reason=approval.reason,
        risk_assessment=approval.risk_assessment,
        expected_outcome=approval.expected_outcome,
        evidence=approval.evidence,
        amount=approval.amount,
        requested_at=approval.requested_at,
        decided_at=approval.decided_at,
        decided_by_user_id=approval.decided_by_user_id,
        decision_note=approval.decision_note,
        consumed_at=approval.consumed_at,
    )


@router.get("", response_model=list[ApprovalOut], summary="List approval requests")
async def list_approvals(
    session: SessionDep,
    principal: ListApprovalsPrincipal,
    status_filter: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ApprovalOut]:
    """Approval requests, oldest first.

    Oldest first on purpose: a queue a human works through should surface the
    request that has been waiting longest, not the most recent one.
    """
    statement = select(Approval).order_by(Approval.requested_at).limit(limit)
    if status_filter:
        statement = statement.where(Approval.status == status_filter)

    approvals = list((await session.execute(statement)).scalars())
    return [_to_out(approval) for approval in approvals]


@router.get("/{approval_id}", response_model=ApprovalOut, summary="Read one approval")
async def get_approval(approval_id: uuid.UUID, session: SessionDep) -> ApprovalOut:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No approval {approval_id}."
        )
    return _to_out(approval)


@router.post(
    "/{approval_id}/decision",
    response_model=ApprovalDecisionOut,
    summary="Record a human decision and resume the workflow",
)
async def decide_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    session: SessionDep,
    runner: RunnerDep,
    policy: PolicyDep,
    principal: DecideApprovalPrincipal,
) -> ApprovalDecisionOut:
    """Record the decision, then resume the paused run.

    Reuses ``WorkflowRunner.resume()`` and the graph's own interrupt mechanism
    rather than introducing a second path to the same outcome. The graph reads
    the decision back from the row this endpoint writes, so there is exactly one
    authority on what a human decided.
    """
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No approval {approval_id}."
        )

    # --- Decidability: is this approval still awaiting a decision? ----------
    # Checked before authority so a second approver racing on an already-decided
    # request gets the accurate reason rather than a role complaint.
    decidable = check_decidable(status=approval.status, consumed_at=approval.consumed_at)
    if not decidable.decidable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": decidable.denial, "message": decidable.message},
        )

    # --- Authority: may this actor decide this? ----------------------------
    # The authenticated principal, re-read for its current roles. Authentication
    # already resolved them; re-reading costs one query and means a role revoked
    # mid-session cannot approve on the strength of a stale token.
    actor = (
        await session.execute(
            select(User).where(User.id == principal.user_id).options(selectinload(User.roles))
        )
    ).scalar_one_or_none()

    authority = check_authority(
        actor_exists=actor is not None,
        actor_is_active=bool(actor and actor.is_active),
        actor_roles=frozenset(role.name for role in actor.roles) if actor else frozenset(),
        amount=approval.amount,
        policy=policy,
    )
    if not authority.permitted:
        # 403, not 401: the caller authenticated successfully. What is refused
        # is authority over *this amount* — re-authenticating would not help.
        logger.warning(
            "approval_decision_refused",
            approval_id=str(approval_id),
            actor_user_id=str(principal.user_id),
            denial=authority.denial,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": authority.denial, "message": authority.message},
        )

    # --- Record: actor and timestamp, committed before anything acts on it --
    decided_at = datetime.now(UTC)
    approval.status = ApprovalStatus.APPROVED if payload.approved else ApprovalStatus.REJECTED
    approval.decided_at = decided_at
    approval.decided_by_user_id = principal.user_id
    approval.decision_note = payload.note

    await record_event(
        session,
        EventType.APPROVAL_RECEIVED,
        actor_type=ActorType.USER,
        actor_id=str(principal.user_id),
        entity_type=approval.entity_type,
        entity_id=approval.entity_id,
        payload={
            "approval_id": str(approval_id),
            "action": approval.action,
            "approved": payload.approved,
            "decided_at": decided_at.isoformat(),
        },
        # Explicit: this endpoint records an event *about* an execution it is
        # not running inside, so the ambient context is not the right answer.
        execution_id=approval.execution_id,
    )
    await session.commit()

    logger.info(
        "approval_decided",
        approval_id=str(approval_id),
        approved=payload.approved,
        actor_user_id=str(principal.user_id),
    )

    # --- Resume: the graph reads the decision back from the row above -------
    outcome = await runner.resume(
        execution_id=approval.execution_id,
        decision={"approval_id": str(approval_id)},
    )

    await session.refresh(approval)
    return ApprovalDecisionOut(
        approval=_to_out(approval),
        workflow_status=outcome.status,
        workflow_resumed=True,
        still_awaiting_approval=outcome.paused,
    )
