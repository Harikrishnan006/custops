"""API tokens — the credential store behind authentication (§17, Phase 13).

Only hashes are stored; the plaintext token exists once, at issuance. See
``domain/models/credential.py`` for why a plain SHA-256 is the right hash for a
256-bit random secret.

Revision ID: 0007_api_tokens
Revises: 0006_workflow_executions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_api_tokens"
down_revision: str | None = "0006_workflow_executions"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        # SHA-256, hex-encoded. Never the token itself.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # Null means "does not expire" — explicit, rather than a far-future
        # sentinel date that would eventually arrive and surprise someone.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # A deleted user's credentials must not outlive them. Audit rows
        # reference users by id and are deliberately *not* foreign-keyed, so
        # deleting a user never erases the record of what they approved.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_api_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_tokens"),
    )
    # Unique: two rows sharing a hash would make revocation ambiguous.
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
