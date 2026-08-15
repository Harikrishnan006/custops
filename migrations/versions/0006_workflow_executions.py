"""Workflow execution and step records.

The readable half of a trace. LangGraph's checkpointer stores what is needed to
resume; these tables store what a human needs to understand what happened, and
join to tool_calls and audit_events by execution_id.

Revision ID: 0006_workflow_executions
Revises: 0005_approvals_and_tool_calls
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_workflow_executions"
down_revision: str | None = "0005_approvals_and_tool_calls"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        # This id IS the execution_id carried through every log line, tool call
        # and audit event (§16) — not a separate correlation key.
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("raw_request", sa.Text(), nullable=False),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("customer_ref", sa.String(length=64), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("target_plan_code", sa.String(length=32), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column(
            "final_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_workflow_executions_account_id_accounts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_executions"),
    )
    op.create_index("ix_workflow_executions_request_id", "workflow_executions", ["request_id"])
    op.create_index(
        "ix_workflow_executions_workflow_type", "workflow_executions", ["workflow_type"]
    )
    op.create_index("ix_workflow_executions_status", "workflow_executions", ["status"])
    op.create_index("ix_workflow_executions_customer_ref", "workflow_executions", ["customer_ref"])

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        # Per visit, not per node: a retry loop visits execute more than once,
        # and collapsing those would hide what the budgets exist to bound.
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(length=64), nullable=False),
        sa.Column(
            "output",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["workflow_executions.id"],
            name="fk_workflow_steps_execution_id_workflow_executions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_steps"),
        sa.UniqueConstraint(
            "execution_id", "sequence", name="uq_workflow_steps_execution_sequence"
        ),
    )
    op.create_index(
        "ix_workflow_steps_execution_id_sequence", "workflow_steps", ["execution_id", "sequence"]
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_steps_execution_id_sequence", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_workflow_executions_customer_ref", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_status", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_workflow_type", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_request_id", table_name="workflow_executions")
    op.drop_table("workflow_executions")
