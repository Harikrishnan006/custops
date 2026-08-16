"""Issuing and revoking API tokens (§17).

Separated from :mod:`custops.apps.api.security.tokens` because this half needs a
database and that half deliberately does not. The split is what lets the
credential rules be tested without infrastructure.

**The plaintext is returned to the caller and never stored.** :func:`issue`
hands it back once; the row holds only the hash. There is no "show me the token
again" operation, because there is nothing to show — reissue instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.apps.api.security.tokens import mint
from custops.domain.models.credential import ApiToken
from custops.domain.models.identity import User
from custops.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TTL_DAYS = 90


class IssuanceError(RuntimeError):
    """A token could not be issued. Never carries a credential."""


@dataclass(frozen=True, slots=True)
class Issued:
    """What the operator is shown, exactly once."""

    token_id: uuid.UUID
    plaintext: str
    label: str
    expires_at: datetime | None

    def __repr__(self) -> str:
        return f"Issued(token_id={self.token_id!r}, label={self.label!r}, plaintext=<redacted>)"


async def issue(
    session: AsyncSession,
    *,
    email: str,
    label: str,
    ttl_days: int | None = DEFAULT_TTL_DAYS,
    now: datetime | None = None,
) -> Issued:
    """Create a token for a user and return the plaintext once.

    ``ttl_days=None`` issues a non-expiring credential. Permitted, because a
    service integration that dies every ninety days at 3am is its own kind of
    incident — but it is an explicit choice rather than the default.
    """
    moment = now or datetime.now(UTC)

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        raise IssuanceError(f"No user with email '{email}'.")
    if not user.is_active:
        # Issuing to a deactivated account would create a credential that
        # authentication refuses anyway — better to say so now.
        raise IssuanceError(f"User '{email}' is not active.")

    minted = mint()
    token = ApiToken(
        id=uuid.uuid4(),
        token_hash=minted.token_hash,
        label=label,
        user_id=user.id,
        expires_at=moment + timedelta(days=ttl_days) if ttl_days is not None else None,
        issued_at=moment,
    )
    session.add(token)
    await session.flush()

    # Note what is absent: no token, no hash. A log line is not a safe place for
    # either, and the hash is enough to authenticate with if the store leaks.
    logger.info(
        "api_token_issued",
        token_id=str(token.id),
        user_id=str(user.id),
        label=label,
        expires_at=token.expires_at.isoformat() if token.expires_at else None,
    )

    return Issued(
        token_id=token.id,
        plaintext=minted.plaintext,
        label=label,
        expires_at=token.expires_at,
    )


async def revoke(
    session: AsyncSession, *, token_id: uuid.UUID, now: datetime | None = None
) -> bool:
    """Withdraw a credential. Returns False if it was already revoked.

    Idempotent by intent but honest about it: revoking twice is not an error,
    and the first revocation's timestamp is kept — when a credential was
    withdrawn is the audit-relevant fact.
    """
    token = await session.get(ApiToken, token_id)
    if token is None:
        raise IssuanceError(f"No token {token_id}.")
    if token.revoked_at is not None:
        return False

    token.revoked_at = now or datetime.now(UTC)
    await session.flush()
    logger.info("api_token_revoked", token_id=str(token_id), user_id=str(token.user_id))
    return True


async def list_for_user(session: AsyncSession, *, email: str) -> list[ApiToken]:
    """Every credential a user holds, so an operator can revoke the right one."""
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        raise IssuanceError(f"No user with email '{email}'.")

    return list(
        (
            await session.execute(
                select(ApiToken)
                .where(ApiToken.user_id == user.id)
                .order_by(ApiToken.issued_at.desc())
            )
        ).scalars()
    )
