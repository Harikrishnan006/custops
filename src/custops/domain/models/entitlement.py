"""Entitlements — mirrored from the legacy provisioning portal.

**This table is not the source of truth.** The legacy portal is (decision D8),
and it has no API, so the only way to change an entitlement is to drive its web
form with a browser. What lives here is a *mirror*, stamped with when it was last
synchronised.

That distinction is the architectural point of the whole system. The billing API
can accept a plan change and return 200 while the entitlement in the portal never
flipped — the customer is billed for Enterprise and provisioned for Professional.
The Validator therefore re-reads the portal itself rather than trusting this
mirror or the executing agent's return value (BUILD_SPEC §14), and
``last_synced_at`` is what makes a stale mirror detectable instead of silently
convincing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin
from custops.domain.enums import EntitlementStatus

if TYPE_CHECKING:
    from custops.domain.models.customer import Account


class Entitlement(Base, TimestampMixin):
    """What the provisioning system believes an account is entitled to."""

    __tablename__ = "entitlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The tier the portal has provisioned. Compared against the subscription's
    # plan during validation; divergence fails the workflow.
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    seats: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EntitlementStatus.PROVISIONED
    )
    # When this mirror was last refreshed from the portal. A mirror without a
    # timestamp cannot be distinguished from a stale one.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[Account] = relationship(back_populates="entitlements")

    __table_args__ = (
        # One entitlement record per account: the portal models a single
        # provisioned tier, not a set.
        UniqueConstraint("account_id", name="uq_entitlements_account_id"),
    )

    def __repr__(self) -> str:
        return f"Entitlement(account_id={self.account_id!r}, tier={self.tier!r})"
