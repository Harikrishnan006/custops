"""Users and roles — the identity spine.

Phase 1 foundation because later phases reference it rather than redefine it:
approvals record *which human* approved an action (Phase 7 / BUILD_SPEC §13),
audit events record the actor (Phase 12), and role-based authorization and
tool-level permissions resolve against these rows (Phase 13 / §17).

No authentication is implemented in Phase 1. These are the tables the eventual
auth layer will read; the login path, password handling and token issuance are
Phase 13 work and are deliberately absent rather than stubbed (Rule 6).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin

# 320 = 64-char local part + "@" + 255-char domain, the RFC-derived maximum.
EMAIL_MAX_LENGTH = 320


class User(Base, TimestampMixin):
    """A human actor. Agents are not users; they are recorded as agent actors."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False, unique=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Deactivation must not delete the row: audit and approval history reference
    # it, and an audit trail with dangling actors is not an audit trail.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles",
        back_populates="users",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r})"


class Role(Base, TimestampMixin):
    """A named set of privileges, resolved by the authorization layer."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    users: Mapped[list[User]] = relationship(
        secondary="user_roles",
        back_populates="roles",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"Role(id={self.id!r}, name={self.name!r})"


class UserRole(Base):
    """User↔role assignment.

    An explicit mapped class rather than a bare ``Table`` so the grant can carry
    its own attribute (``granted_at``) — who held which role when is an audit
    question, and association tables that need columns later are painful to
    convert after the fact.
    """

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
