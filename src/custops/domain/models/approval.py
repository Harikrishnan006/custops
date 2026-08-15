"""Approvals and tool-call records.

These tables exist in Phase 4 rather than Phase 7 because the **tool layer** is
what enforces approval (decision D9), and enforcement needs something to verify
against. Phase 7 adds the human-facing API and the graph's interrupt; this is
the record they write to and the tools read from.

The ordering matters for a reason worth stating: if the tables arrived with the
approval API, the tools would have been written first against nothing, and
"verify an approval exists" would have started life as a TODO. Building the
boundary before the happy path is what stops the boundary from being optional.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from custops.db.base import Base, TimestampMixin


class ApprovalStatus(StrEnum):
    """Lifecycle of an approval request.

    Only ``APPROVED`` authorises a mutation. Everything else — including
    ``PENDING`` — is a refusal from the tool layer's point of view, which is why
    the check is an equality test against APPROVED rather than a not-REJECTED
    test. A status added later (say, ``EXPIRED``) is then denied by default
    instead of silently authorising.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Approval(Base, TimestampMixin):
    """A human decision on a proposed high-risk action (BUILD_SPEC §13)."""

    __tablename__ = "approvals"
    __table_args__ = (
        # The lookup every mutating tool performs before acting.
        Index("ix_approvals_execution_id_action", "execution_id", "action"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)

    # Scopes the approval to one workflow run. An approval granted for one
    # execution must never authorise a different one — that is the difference
    # between "a human approved this upgrade" and "a human once approved an
    # upgrade".
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)

    # What the tool is about to touch. Checked alongside the action so an
    # approval for account A cannot authorise the same action on account B.
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ApprovalStatus.PENDING, index=True
    )

    # The request, as presented to the human: reason, risk assessment,
    # supporting evidence citations, expected outcome (§13).
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    risk_assessment: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Who decided, and when. An approval trail without an actor is not a trail.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when the approval has been spent. Prevents one human decision from
    # authorising a retry loop's worth of mutations.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"Approval(execution_id={self.execution_id!r}, action={self.action!r}, "
            f"status={self.status!r})"
        )


class ToolCall(Base):
    """Append-only record of every tool invocation (BUILD_SPEC §8).

    Written by mutating tools before *and* after the attempt, so a call that
    crashed mid-flight is distinguishable from one that never started — the
    question the Validator asks when state looks half-applied.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_execution_id_started_at", "execution_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Tool I/O, stored where safe (§16). Never chain-of-thought.
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"ToolCall(tool_name={self.tool_name!r}, succeeded={self.succeeded!r})"
