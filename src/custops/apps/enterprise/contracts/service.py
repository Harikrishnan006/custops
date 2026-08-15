"""Contract and policy lookups."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.domain.enums import ContractStatus
from custops.domain.models.contract import Contract, Policy


async def get_active_contract(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime
) -> Contract | None:
    """The contract in force for an account right now.

    Both the status *and* the dates are checked. A contract row left as 'active'
    past its end date is a data-quality problem that must not silently authorise
    a change; requiring the dates to agree means such a row simply stops matching
    rather than granting permission it no longer has.
    """
    statement = (
        select(Contract)
        .where(
            Contract.account_id == account_id,
            Contract.status == ContractStatus.ACTIVE,
            Contract.starts_at <= now,
            Contract.ends_at > now,
        )
        .order_by(Contract.starts_at.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def get_latest_contract(session: AsyncSession, account_id: uuid.UUID) -> Contract | None:
    """The most recent contract regardless of status, for evidence and audit."""
    statement = (
        select(Contract)
        .where(Contract.account_id == account_id)
        .order_by(Contract.starts_at.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def get_policy(session: AsyncSession, code: str, *, now: datetime) -> Policy | None:
    """The version of a policy in force at ``now``.

    Policies are versioned rather than mutated, so "which version applied when
    this decision was made" stays answerable after the policy changes.
    """
    statement = (
        select(Policy)
        .where(
            Policy.code == code,
            Policy.effective_from <= now,
            (Policy.effective_to.is_(None)) | (Policy.effective_to > now),
        )
        .order_by(Policy.version.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def list_policies(session: AsyncSession, *, category: str | None = None) -> list[Policy]:
    statement = select(Policy).order_by(Policy.code, Policy.version.desc())
    if category is not None:
        statement = statement.where(Policy.category == category)
    return list((await session.execute(statement)).scalars())
