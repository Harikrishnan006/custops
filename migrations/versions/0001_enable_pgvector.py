"""Enable the pgvector extension.

Its own migration, ahead of any table, because it is a database *capability*
rather than schema: the knowledge layer's vector column (Phase 3) cannot be
created until the extension exists, and separating them keeps the failure legible
when a server without pgvector installed is targeted.

pgvector is an extension of the one PostgreSQL instance, not a second service
(decision D4). ``CREATE EXTENSION`` requires elevated privileges; Phase 13
introduces a least-privilege application role distinct from the migration role.

Revision ID: 0001_enable_pgvector
Revises: None
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_enable_pgvector"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

EXTENSION_NAME = "vector"


def upgrade() -> None:
    op.execute(f"CREATE EXTENSION IF NOT EXISTS {EXTENSION_NAME}")


def downgrade() -> None:
    # Dropping the extension would drop dependent vector columns. Safe here only
    # because 0001 is the first revision: nothing above it can own vector data
    # once the downgrade chain has reached this point.
    op.execute(f"DROP EXTENSION IF EXISTS {EXTENSION_NAME}")
