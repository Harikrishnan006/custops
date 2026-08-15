"""Support tickets and their conversations.

Support history is evidence, not decoration: an account with three unresolved
urgent tickets is a different upgrade conversation from one with none, and the
Research agent surfaces that as structured evidence with source references.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin
from custops.domain.enums import AuthorType, TicketPriority, TicketStatus

if TYPE_CHECKING:
    from custops.domain.models.customer import Account


class SupportTicket(Base, TimestampMixin):
    """One reported issue."""

    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TicketStatus.OPEN, index=True
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TicketPriority.NORMAL, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    satisfaction_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    account: Mapped[Account] = relationship(back_populates="tickets")
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="Conversation.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "satisfaction_score IS NULL OR (satisfaction_score BETWEEN 1 AND 5)",
            name="satisfaction_in_range",
        ),
    )

    def __repr__(self) -> str:
        return f"SupportTicket(reference={self.reference!r}, status={self.status!r})"


class Conversation(Base):
    """One message on a ticket.

    ``author_type`` distinguishes customer, human agent and system, which matters
    for evidence: "the customer said the integration is blocking them" and "an
    automated reply was sent" are not the same fact.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AuthorType.CUSTOMER
    )
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    ticket: Mapped[SupportTicket] = relationship(back_populates="conversations")

    def __repr__(self) -> str:
        return f"Conversation(ticket_id={self.ticket_id!r}, author_type={self.author_type!r})"
