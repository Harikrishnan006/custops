"""Customer, account and contact — the CRM side of the domain.

The three-level shape (customer → account → contact) is what B2B SaaS actually
looks like: one commercial relationship can hold several billable accounts
(subsidiaries, regions, product lines), and each account has people attached to
it. Collapsing it to a single "customer" table would make the Subscription
Upgrade workflow ambiguous about *what* is being upgraded.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin
from custops.domain.enums import AccountStatus, CustomerStatus

if TYPE_CHECKING:
    from custops.domain.models.billing import Discount, Invoice, Subscription
    from custops.domain.models.contract import Contract
    from custops.domain.models.entitlement import Entitlement
    from custops.domain.models.support import SupportTicket


class Customer(Base, TimestampMixin):
    """A commercial relationship."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    # Stable, human-quotable handle ("ACME"). Workflows are initiated by people
    # who say "upgrade Acme", not "upgrade 5f3e...".
    external_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CustomerStatus.ACTIVE, index=True
    )

    accounts: Mapped[list[Account]] = relationship(
        back_populates="customer",
        cascade="all, delete-orphan",
        # The FK already declares ON DELETE CASCADE. Without this the ORM loads
        # every child and de-associates it by writing NULL into a NOT NULL
        # column — see the note on Account's collections below.
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"Customer(external_ref={self.external_ref!r}, name={self.name!r})"


class Account(Base, TimestampMixin):
    """A billable unit. Subscriptions, invoices and entitlements hang off this."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AccountStatus.ACTIVE, index=True
    )
    billing_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # ISO 4217. Stored per account because pricing and invoices must never mix
    # currencies within one account.
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    # --- The CRM's own copy of the plan -------------------------------------
    # Denormalised on purpose, and realistic: CRMs cache the plan for pipeline
    # and reporting rather than joining into billing on every read.
    #
    # This is the third place a plan is recorded, and the three can disagree:
    #   subscriptions.plan_id  → what billing charges for   (billing truth)
    #   entitlements.tier      → what is provisioned        (portal truth, D8)
    #   accounts.current_plan_code → what the CRM believes  (this field)
    #
    # A successful upgrade updates all three. The Validator re-reads all three
    # and fails on divergence (BUILD_SPEC §14) — which is only possible because
    # they are genuinely separate records rather than one field read three ways.
    current_plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifecycle_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="established")
    last_plan_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    customer: Mapped[Customer] = relationship(back_populates="accounts")

    # Every collection below is *owned* by the account: its rows carry a
    # NOT NULL ``account_id`` whose foreign key already says ON DELETE CASCADE.
    #
    # ``passive_deletes=True`` is what makes the ORM honour that. Without it
    # SQLAlchemy's default on parent deletion is to load each child and
    # *de-associate* it — writing NULL into a NOT NULL column, which PostgreSQL
    # rejects:
    #
    #     NotNullViolationError: null value in column "account_id"
    #     of relation "discounts" violates not-null constraint
    #
    # ``contacts`` carried the cascade from the start and so never failed; the
    # other six did not, and nothing noticed until the first CI run deleted a
    # seeded customer against a real database. It produced 72 teardown errors
    # from one missing keyword.
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    invoices: Mapped[list[Invoice]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    contracts: Mapped[list[Contract]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    discounts: Mapped[list[Discount]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    tickets: Mapped[list[SupportTicket]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    entitlements: Mapped[list[Entitlement]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"Account(id={self.id!r}, name={self.name!r}, status={self.status!r})"


class Contact(Base, TimestampMixin):
    """A person at the customer. Notifications resolve to the primary contact."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    account: Mapped[Account] = relationship(back_populates="contacts")

    def __repr__(self) -> str:
        return f"Contact(email={self.email!r}, is_primary={self.is_primary!r})"
