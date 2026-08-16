"""API tokens — the credential behind authentication (§17).

**Only the hash is stored.** The plaintext token exists exactly once, in the
output of ``custops issue-token``, and is never written to this table, to a log
line, to an audit payload, or to an HTTP response. A database dump therefore
discloses nothing usable, which is the whole point of hashing a credential.

**Why a plain SHA-256 rather than a password KDF.** bcrypt, scrypt and argon2
exist to make brute force expensive against *low-entropy* secrets — human-chosen
passwords. These tokens are 256 bits of ``secrets.token_urlsafe`` output; there
is no dictionary to try and no feasible search space, so a slow KDF would buy
nothing and cost a KDF evaluation on every single request. The security comes
from the entropy of the token, not from the cost of the hash.

Deliberately a separate table rather than a column on ``users``:

* one user may hold several tokens — a person's CLI and a service integration
  acting on their behalf are different credentials with different lifetimes;
* revoking one must not disturb the others;
* the token's own lifecycle (issued, last used, expired, revoked) is not
  user identity and does not belong in the identity row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin
from custops.domain.models.identity import User

# SHA-256, hex-encoded.
TOKEN_HASH_LENGTH = 64

# A human-readable handle so an operator can revoke the right token without
# ever seeing the secret. Not unique: two people may both call one "laptop".
TOKEN_LABEL_MAX_LENGTH = 64


class ApiToken(Base, TimestampMixin):
    """A bearer credential belonging to one user."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        # The authentication lookup: hash first, then validity. Unique because
        # two rows sharing a hash would make revocation ambiguous.
        Index("ix_api_tokens_token_hash", "token_hash", unique=True),
        Index("ix_api_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)

    # Never the token itself. See the module docstring.
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), nullable=False)

    label: Mapped[str] = mapped_column(String(TOKEN_LABEL_MAX_LENGTH), nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        # Cascade: a deleted user's credentials must not outlive them. Audit
        # rows reference users by id and are deliberately not foreign-keyed, so
        # deleting a user does not erase the record of what they approved.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Null means "does not expire". Explicit rather than a sentinel far-future
    # date, which would eventually arrive and surprise someone.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set once, never cleared: un-revoking a credential that may have leaked is
    # not an operation anyone should have.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Written on use so an operator can find credentials nobody uses any more.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(lazy="selectin")

    def __repr__(self) -> str:
        # Note the absence of token_hash: a repr lands in logs and tracebacks,
        # and even a hash is a credential-shaped thing not worth leaking.
        return f"ApiToken(id={self.id!r}, label={self.label!r}, user_id={self.user_id!r})"
