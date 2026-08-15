"""Support operations.

Support data enters workflows as *evidence*, so these functions return counts and
structured summaries rather than prose. Turning "three unresolved urgent tickets
about the API integration" into a sentence is the Research agent's job; producing
the fact is this module's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from custops.domain.enums import TicketPriority, TicketStatus
from custops.domain.models.support import SupportTicket

# Statuses meaning the ticket is still live.
UNRESOLVED_STATUSES = (TicketStatus.OPEN, TicketStatus.PENDING)


@dataclass(frozen=True, slots=True)
class SupportSummary:
    """Counts a decision can be based on, without loading every ticket."""

    total_tickets: int
    unresolved_tickets: int
    open_urgent_tickets: int
    average_satisfaction: float | None


async def get_support_history(
    session: AsyncSession, account_id: uuid.UUID, limit: int = 20
) -> list[SupportTicket]:
    statement = (
        select(SupportTicket)
        .where(SupportTicket.account_id == account_id)
        .options(selectinload(SupportTicket.conversations))
        .order_by(SupportTicket.opened_at.desc())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars())


async def count_open_urgent_tickets(session: AsyncSession, account_id: uuid.UUID) -> int:
    statement = (
        select(func.count())
        .select_from(SupportTicket)
        .where(
            SupportTicket.account_id == account_id,
            SupportTicket.status.in_(UNRESOLVED_STATUSES),
            SupportTicket.priority == TicketPriority.URGENT,
        )
    )
    return int((await session.execute(statement)).scalar_one())


async def summarise_support(session: AsyncSession, account_id: uuid.UUID) -> SupportSummary:
    """Aggregate an account's support posture in one round trip."""
    statement = select(
        func.count(SupportTicket.id),
        func.count(SupportTicket.id).filter(SupportTicket.status.in_(UNRESOLVED_STATUSES)),
        func.count(SupportTicket.id).filter(
            SupportTicket.status.in_(UNRESOLVED_STATUSES),
            SupportTicket.priority == TicketPriority.URGENT,
        ),
        func.avg(SupportTicket.satisfaction_score),
    ).where(SupportTicket.account_id == account_id)

    total, unresolved, urgent, average = (await session.execute(statement)).one()

    return SupportSummary(
        total_tickets=int(total or 0),
        unresolved_tickets=int(unresolved or 0),
        open_urgent_tickets=int(urgent or 0),
        average_satisfaction=float(average) if average is not None else None,
    )
