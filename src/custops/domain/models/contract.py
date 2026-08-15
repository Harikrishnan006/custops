"""Contracts and policies.

Both are the *interpretable* side of the domain: a contract carries structured
fields the deterministic rules read (term dates, restriction, notice period) and
a document body an LLM interprets when the structured fields are not enough.

That split is the point. ``upgrade_restriction`` is machine-checkable and is
checked in Python; the prose in ``document_body`` is what the Research agent
retrieves and cites when a human needs to understand *why* a restriction
applies. The rule never asks the model whether an upgrade is permitted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin
from custops.domain.enums import ContractStatus, UpgradeRestriction

if TYPE_CHECKING:
    from custops.domain.models.customer import Account


class Contract(Base, TimestampMixin):
    """A signed commercial agreement governing an account."""

    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ContractStatus.ACTIVE, index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    minimum_term_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    notice_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # Structured, machine-checkable. Read by domain/rules/eligibility.py.
    upgrade_restriction: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UpgradeRestriction.NONE
    )
    # A negotiated ceiling on price increases at renewal, if any.
    price_increase_cap_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Prose. Interpreted by the Research agent, cited as evidence, never used as
    # the authority for an authorization decision.
    document_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[Account] = relationship(back_populates="contracts")

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="term_ordered"),
        CheckConstraint("minimum_term_months >= 0", name="minimum_term_non_negative"),
    )

    def __repr__(self) -> str:
        return (
            f"Contract(number={self.contract_number!r}, status={self.status!r}, "
            f"restriction={self.upgrade_restriction!r})"
        )


class Policy(Base, TimestampMixin):
    """An internal operating policy.

    Policies are versioned and dated because "what did the policy say when this
    decision was made" is an audit question. They are also the primary corpus for
    Phase 3's knowledge ingestion.
    """

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One row per (code, version): re-issuing a policy creates a new version
        # rather than mutating history.
        CheckConstraint("version >= 1", name="version_positive"),
    )

    def __repr__(self) -> str:
        return f"Policy(code={self.code!r}, version={self.version!r})"
