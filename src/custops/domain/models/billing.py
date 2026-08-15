"""Plans, subscriptions, invoices, payments and discounts.

**Money is ``Numeric``, never ``Float``.** Binary floating point cannot represent
0.10 exactly; a proration credit computed in floats and compared against a
recomputed value is a validation failure waiting to happen, and this system
validates every financial action by recomputing it (BUILD_SPEC §14). The
SQLAlchemy type maps to ``Decimal`` in Python, and the rules in
``domain/rules/pricing.py`` do all arithmetic in ``Decimal``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin
from custops.domain.enums import (
    BillingCycle,
    InvoiceStatus,
    PaymentStatus,
    SubscriptionStatus,
)

if TYPE_CHECKING:
    from custops.domain.models.customer import Account

# 12 digits total, 2 after the point: up to 9,999,999,999.99 — comfortably
# beyond any B2B SaaS invoice, without inviting float-shaped shortcuts.
MONEY = Numeric(12, 2)


class Plan(Base, TimestampMixin):
    """A purchasable tier.

    ``rank`` is what makes "is this an upgrade?" a deterministic comparison
    rather than string matching on tier names. Without it, the eligibility rule
    would have to hardcode an ordering, and adding a tier would mean editing
    business logic instead of inserting a row.
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    annual_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # Short names: the metadata convention prefixes these with
        # "ck_<table>_", so a name repeating the table would render doubled.
        CheckConstraint("monthly_price >= 0", name="monthly_price_non_negative"),
        CheckConstraint("annual_price >= 0", name="annual_price_non_negative"),
    )

    def __repr__(self) -> str:
        return f"Plan(code={self.code!r}, rank={self.rank!r})"


class Subscription(Base, TimestampMixin):
    """An account's current plan and billing period.

    The period boundaries are stored rather than derived: proration for a
    mid-cycle upgrade depends on exactly how much of *this* period remains, and
    recomputing period boundaries from a start date plus a cycle silently
    disagrees with the billing system across month lengths.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("plans.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SubscriptionStatus.ACTIVE, index=True
    )
    billing_cycle: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BillingCycle.MONTHLY
    )
    seats: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    account: Mapped[Account] = relationship(back_populates="subscriptions")
    plan: Mapped[Plan] = relationship(lazy="selectin")

    __table_args__ = (
        CheckConstraint("seats >= 1", name="seats_positive"),
        CheckConstraint(
            "current_period_end > current_period_start",
            name="period_ordered",
        ),
    )

    def __repr__(self) -> str:
        return f"Subscription(id={self.id!r}, status={self.status!r}, seats={self.seats!r})"


class Invoice(Base, TimestampMixin):
    """A billing document. Proration from an upgrade lands here."""

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=InvoiceStatus.DRAFT, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    amount_due: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0.00"))
    amount_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0.00"))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    account: Mapped[Account] = relationship(back_populates="invoices")

    def __repr__(self) -> str:
        return f"Invoice(number={self.number!r}, status={self.status!r}, due={self.amount_due!r})"


class Payment(Base, TimestampMixin):
    """Money actually received against an invoice."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaymentStatus.PENDING, index=True
    )
    method: Mapped[str] = mapped_column(String(32), nullable=False, default="card")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    invoice: Mapped[Invoice] = relationship()

    def __repr__(self) -> str:
        return f"Payment(amount={self.amount!r}, status={self.status!r})"


class Discount(Base, TimestampMixin):
    """A negotiated price reduction.

    ``percent_off`` above the configured threshold is one of the trigger
    conditions for human approval (BUILD_SPEC §13), which is why
    ``approved_by_user_id`` exists on the row: a discount that was never approved
    is distinguishable from one that was, after the fact.
    """

    __tablename__ = "discounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    percent_off: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    account: Mapped[Account] = relationship(back_populates="discounts")

    __table_args__ = (
        CheckConstraint(
            "percent_off >= 0 AND percent_off <= 100",
            name="percent_off_in_range",
        ),
    )

    def __repr__(self) -> str:
        return f"Discount(code={self.code!r}, percent_off={self.percent_off!r})"
