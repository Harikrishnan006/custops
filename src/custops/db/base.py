"""Declarative base and shared column mixins.

The naming convention is the load-bearing part of this module. Without it,
PostgreSQL invents constraint and index names, Alembic's autogenerate compares
them against nothing, and migrations start producing spurious drop/create pairs.
Fixing the convention before the first table is created is far cheaper than
renaming constraints later.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# %(column_0_N_name)s covers composite indexes/constraints; %(constraint_name)s
# requires check constraints to be named explicitly at the call site.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the platform."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` / ``updated_at``, both timezone-aware.

    Defaults are server-side (``now()``): the database clock is the one clock
    every writer shares, including migrations and manual SQL.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
