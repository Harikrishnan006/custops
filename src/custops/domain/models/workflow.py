"""Workflow execution records (BUILD_SPEC §5, §16).

These are the persistence spine of a trace. LangGraph's checkpointer already
stores enough to *resume* a run; it does not store anything a human can read to
answer "what happened, and why". These tables do.

The division against the checkpointer is deliberate and worth stating: the
checkpointer is the library's resumption state, versioned with the library and
opaque by design. This is our audit record — queryable, stable, and joined to
``tool_calls`` and ``audit_events`` by ``execution_id``.

``agent_runs`` from §5 is deliberately absent. The provider layer does not yet
report token usage, so the table would be mostly nulls pretending to be
telemetry. It arrives in Phase 12 with the observability work that can populate
it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin


class WorkflowExecution(Base, TimestampMixin):
    """One run of one workflow.

    ``id`` *is* the ``execution_id`` that propagates through every log line,
    tool call and audit event (§16). Making them the same value rather than
    carrying a separate correlation key is what keeps a trace joinable without a
    lookup table.
    """

    __tablename__ = "workflow_executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    raw_request: Mapped[str] = mapped_column(Text, nullable=False)

    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Subject of the run, resolved during research. Nullable because a run that
    # escalates at classification never gets this far.
    customer_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    target_plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Budgets as actually spent, so "why did this escalate?" is answerable from
    # the row rather than by replaying the graph.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Structured final state: decisions, evidence citations, validation results.
    # Never chain-of-thought (Rule 18).
    final_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.sequence",
    )

    def __repr__(self) -> str:
        return f"WorkflowExecution(id={self.id!r}, status={self.status!r})"


class WorkflowStep(Base):
    """One node visit within a run.

    Recorded per visit rather than per node: a retry loop visits ``execute``
    more than once, and collapsing those would hide exactly the behaviour the
    budgets exist to bound. ``sequence`` preserves the order a human reads.
    """

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_workflow_steps_execution_sequence"),
        Index("ix_workflow_steps_execution_id_sequence", "execution_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)

    # The partial state update the node produced, structured. This is what makes
    # a trace explanatory rather than merely chronological.
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    execution: Mapped[WorkflowExecution] = relationship(back_populates="steps")

    def __repr__(self) -> str:
        return f"WorkflowStep(node={self.node!r}, sequence={self.sequence!r})"
