"""The audit spine.

Append-only record of everything the platform did, keyed by ``execution_id`` so
a complete workflow trace can be reconstructed after the fact (BUILD_SPEC §16).
Phase 12 builds the inspection endpoint on top of exactly this table; Phase 4
requires every mutating MCP tool to write a row here.

Two constraints on ``payload`` that matter more than the schema:

* **Never chain-of-thought** (Rule 18, §16). Structured decisions, evidence
  references, tool input/output where safe, and concise rationale summaries only.
* **Never secrets.** Rows are read by an inspection endpoint; anything written
  here is effectively disclosed to whoever can see a trace.

Nothing writes to this table in Phase 1 — no workflows exist yet. The table and
its contract are foundation; the write path is Phase 12.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from custops.db.base import Base


class AuditEvent(Base):
    """One audited occurrence.

    Deliberately *not* a foreign key onto workflow tables: audit rows must
    survive the deletion of anything they describe, and an audit write must never
    fail because a referenced row is missing. ``execution_id`` is an indexed
    correlation key, not a constraint.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        # The trace-reconstruction query: all events for one execution, ordered.
        Index("ix_audit_events_execution_id_occurred_at", "execution_id", "occurred_at"),
        Index("ix_audit_events_entity_type_entity_id", "entity_type", "entity_id"),
    )

    # Monotonic identity rather than a UUID: audit is read in insertion order,
    # and IDENTITY is the modern PostgreSQL form of a generated key.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Nullable because not every audited action belongs to a workflow (an
    # administrative login does not), and indexed because trace reconstruction
    # always filters on it.
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Values come from observability.events.EventType / ActorType. Enforced in
    # the application layer, stored as text — see that module for why.
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )

    def __repr__(self) -> str:
        return (
            f"AuditEvent(id={self.id!r}, event_type={self.event_type!r}, "
            f"execution_id={self.execution_id!r})"
        )
