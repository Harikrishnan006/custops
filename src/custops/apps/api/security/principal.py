"""Establishing who is calling, before a protected endpoint runs (§17).

The dependency here is the *only* way a request acquires an identity. Nothing
downstream reads a caller-supplied user id, which is the change Phase 13 exists
to make: before it, ``actor_user_id`` on an approval decision was asserted by
whoever sent the request.

**Failures are logged, never audited as a new event type.** §16 fixes a closed
vocabulary of nineteen workflow events, and an authentication failure is not one
of them — it belongs to no execution and describes no step of a workflow. It
goes to structlog with a reason code.

**No bypass exists.** Tests authenticate through this same dependency with real
token rows. An "auth disabled in test" switch is precisely the flag that
eventually ships enabled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.apps.api.security.tokens import (
    AUTH_SCHEME,
    TokenError,
    check_validity,
    extract_bearer,
    hash_token,
)
from custops.apps.enterprise.router import get_session
from custops.domain.models.credential import ApiToken
from custops.domain.policies.endpoint_authority import EndpointAction, get_policy, is_permitted
from custops.observability.logging import get_logger

logger = get_logger(__name__)

# Returned on every 401 so a client knows how to authenticate, per RFC 7235.
_AUTH_CHALLENGE = {"WWW-Authenticate": AUTH_SCHEME}


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    Frozen, and carrying the roles resolved at authentication time. Endpoints
    read identity from this and from nothing else — the whole point being that
    a request body can no longer claim to be someone.
    """

    user_id: uuid.UUID
    email: str
    roles: frozenset[str]
    token_id: uuid.UUID

    def has_role(self, role: str) -> bool:
        return role in self.roles


def _unauthenticated(reason: str) -> HTTPException:
    """A 401 that says nothing useful to an attacker.

    The reason is logged, not returned: telling a caller whether a token was
    unknown, expired or revoked distinguishes a valid-but-stale credential from
    a guess, which is information worth withholding.
    """
    logger.warning("authentication_failed", reason=reason)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers=_AUTH_CHALLENGE,
    )


async def get_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the bearer token into an authenticated principal, or refuse.

    Looked up by hash: the plaintext is never stored, so the only way to find a
    row is to hash what was presented and match it.
    """
    try:
        supplied = extract_bearer(authorization)
    except TokenError as error:
        raise _unauthenticated(str(error)) from None

    token = (
        await session.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_token(supplied))
        )
    ).scalar_one_or_none()

    if token is None:
        raise _unauthenticated("no_such_token")

    now = datetime.now(UTC)
    validity = check_validity(
        revoked_at=token.revoked_at,
        expires_at=token.expires_at,
        user_is_active=token.user.is_active,
        now=now,
    )
    if not validity.valid:
        raise _unauthenticated(validity.reason or "invalid_token")

    # Recorded so an operator can find credentials nobody uses any more. Not
    # flushed explicitly: the request's own transaction carries it, and an
    # authentication that failed to commit a timestamp must not fail the call.
    token.last_used_at = now

    return Principal(
        user_id=token.user_id,
        email=token.user.email,
        roles=frozenset(role.name for role in token.user.roles),
        token_id=token.id,
    )


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require(action: EndpointAction) -> Any:
    """Build a dependency that authenticates *and* authorises.

    An endpoint declares one parameter and gets both guarantees — there is no
    way to depend on the authorisation without also receiving the identity it
    authorised, which is what stops the two drifting apart.

    Typed ``Any`` rather than ``Principal`` because what this returns is
    FastAPI's ``Depends`` marker; the framework substitutes the real
    ``Principal`` when the request is handled. Annotating it as ``Principal``
    would be a more convenient lie, and the call sites are annotated correctly
    either way.
    """

    async def _guard(principal: CurrentPrincipal) -> Principal:
        if not is_permitted(principal.roles, action):
            policy = get_policy(action)
            # 403, not 401: the caller proved who they are; what they lack is
            # permission, and re-authenticating would not help.
            logger.warning(
                "endpoint_authorization_denied",
                action=str(action),
                user_id=str(principal.user_id),
                required_roles=sorted(policy.allowed_roles),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "insufficient_role",
                    "message": f"Action '{action}' requires one of: "
                    f"{', '.join(sorted(policy.allowed_roles))}.",
                },
            )
        return principal

    return Depends(_guard)


# Ready-made dependencies, one per action. Module-level singletons rather than
# `require(...)` called in an argument default: a call in a default is evaluated
# once at import anyway, and writing it inline invites the reader to think a
# fresh dependency is built per request.
StartWorkflowPrincipal = Annotated[Principal, require(EndpointAction.START_WORKFLOW)]
ReadWorkflowPrincipal = Annotated[Principal, require(EndpointAction.READ_WORKFLOW)]
ListApprovalsPrincipal = Annotated[Principal, require(EndpointAction.LIST_APPROVALS)]
ReadApprovalPrincipal = Annotated[Principal, require(EndpointAction.READ_APPROVAL)]
DecideApprovalPrincipal = Annotated[Principal, require(EndpointAction.DECIDE_APPROVAL)]
