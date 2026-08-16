"""Authentication and endpoint authorization, end to end over HTTP (§17).

Runs without PostgreSQL by substituting **the session only**. The dependency
under test — ``get_principal`` and the ``require(...)`` guard — is the real one,
wired into a real FastAPI app and reached over a real ASGI transport. Nothing
about authentication is stubbed, which is the point: an "auth disabled in test"
switch is precisely the flag that eventually ships enabled.

What the fake session stands in for is the row store, so these tests can express
"a revoked token exists" without needing a database to put it in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from custops.apps.api.security.principal import Principal, require
from custops.apps.api.security.tokens import hash_token, mint
from custops.apps.enterprise.router import get_session
from custops.domain.policies.endpoint_authority import EndpointAction

NOW = datetime.now(UTC)


@dataclass
class FakeRole:
    name: str


@dataclass
class FakeUser:
    id: uuid.UUID
    email: str
    is_active: bool = True
    roles: list[FakeRole] = field(default_factory=list)


@dataclass
class FakeToken:
    id: uuid.UUID
    token_hash: str
    user_id: uuid.UUID
    user: FakeUser
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    """Returns one token row for any query, or none.

    Deliberately crude: what is being tested is the dependency's decisions, not
    SQLAlchemy's ability to filter.
    """

    def __init__(self, token: FakeToken | None) -> None:
        self._token = token

    async def execute(self, *_: Any, **__: Any) -> FakeResult:
        return FakeResult(self._token)


def _user(*roles: str, active: bool = True) -> FakeUser:
    return FakeUser(
        id=uuid.uuid4(),
        email="someone@custops.example.com",
        is_active=active,
        roles=[FakeRole(name) for name in roles],
    )


def _app(token: FakeToken | None, action: EndpointAction) -> FastAPI:
    """A real app with one protected route, guarded by the real dependency."""
    app = FastAPI()

    @app.get("/protected")
    async def protected(principal: Principal = require(action)) -> dict[str, Any]:  # noqa: B008
        return {"user_id": str(principal.user_id), "roles": sorted(principal.roles)}

    @app.post("/protected")
    async def protected_post(
        payload: dict[str, Any],
        principal: Principal = require(action),  # noqa: B008
    ) -> dict[str, Any]:
        # Echoes the principal, never the body's claim — the assertion below
        # depends on this being the only identity the handler can reach.
        return {"actor": str(principal.user_id)}

    app.dependency_overrides[get_session] = lambda: FakeSession(token)
    return app


async def _get(app: FastAPI, headers: dict[str, str] | None = None) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/protected", headers=headers or {})


def _live_token(*roles: str, active: bool = True) -> tuple[FakeToken, str]:
    minted = mint()
    user = _user(*roles, active=active)
    token = FakeToken(
        id=uuid.uuid4(),
        token_hash=minted.token_hash,
        user_id=user.id,
        user=user,
        expires_at=NOW + timedelta(days=30),
    )
    return token, minted.plaintext


# ------------------------------------------------------------------- 401 paths


async def test_a_request_without_a_token_is_unauthenticated() -> None:
    response = await _get(_app(None, EndpointAction.READ_WORKFLOW))

    assert response.status_code == 401


async def test_the_challenge_header_tells_a_client_how_to_authenticate() -> None:
    """RFC 7235: a 401 without a challenge leaves a client guessing."""
    response = await _get(_app(None, EndpointAction.READ_WORKFLOW))

    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["", "custops_abc", "Basic dXNlcjpwYXNz", "Bearer", "Token custops_abc"],
)
async def test_a_malformed_authorization_header_is_unauthenticated(header: str) -> None:
    response = await _get(
        _app(None, EndpointAction.READ_WORKFLOW), {"Authorization": header}
    )

    assert response.status_code == 401


async def test_an_unknown_token_is_unauthenticated() -> None:
    """No row matches the presented hash."""
    response = await _get(
        _app(None, EndpointAction.READ_WORKFLOW),
        {"Authorization": "Bearer custops_never_issued"},
    )

    assert response.status_code == 401


async def test_an_expired_token_is_refused() -> None:
    token, plaintext = _live_token("viewer")
    token.expires_at = NOW - timedelta(seconds=1)

    response = await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 401


async def test_a_revoked_token_is_refused() -> None:
    token, plaintext = _live_token("viewer")
    token.revoked_at = NOW - timedelta(days=1)

    response = await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 401


async def test_a_deactivated_user_cannot_authenticate() -> None:
    token, plaintext = _live_token("viewer", active=False)

    response = await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 401


async def test_a_401_body_does_not_say_why() -> None:
    """Distinguishing "expired" from "unknown" tells an attacker whether a
    guessed token ever existed. The reason is logged, not returned."""
    token, plaintext = _live_token("viewer")
    token.revoked_at = NOW

    response = await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert "revoked" not in response.text.lower()
    assert "expired" not in response.text.lower()


# ------------------------------------------------------------------- 403 paths


async def test_an_authenticated_caller_without_the_role_is_forbidden() -> None:
    """401 says "who are you?"; 403 says "not you". Re-authenticating would
    not help, and the status should say so."""
    token, plaintext = _live_token("viewer")

    response = await _get(
        _app(token, EndpointAction.START_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_role"


async def test_the_forbidden_response_names_the_roles_that_would_work() -> None:
    """Actionable for a legitimate caller; useless to an attacker, who cannot
    grant themselves a role."""
    token, plaintext = _live_token("viewer")

    response = await _get(
        _app(token, EndpointAction.START_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert "operator" in response.json()["detail"]["message"]


async def test_a_caller_holding_no_roles_is_forbidden() -> None:
    token, plaintext = _live_token()

    response = await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 403


# ------------------------------------------------------------------ 200 paths


async def test_a_valid_token_with_the_right_role_is_admitted() -> None:
    token, plaintext = _live_token("operator")

    response = await _get(
        _app(token, EndpointAction.START_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(token.user_id)


async def test_a_token_without_an_expiry_is_admitted() -> None:
    token, plaintext = _live_token("viewer")
    token.expires_at = None

    response = await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 200


async def test_the_principal_carries_the_roles_resolved_at_authentication() -> None:
    token, plaintext = _live_token("operator", "approver")

    response = await _get(
        _app(token, EndpointAction.START_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert sorted(response.json()["roles"]) == ["approver", "operator"]


async def test_using_a_token_records_that_it_was_used() -> None:
    """So an operator can find credentials nobody uses any more."""
    token, plaintext = _live_token("viewer")

    await _get(
        _app(token, EndpointAction.READ_WORKFLOW), {"Authorization": f"Bearer {plaintext}"}
    )

    assert token.last_used_at is not None


# ------------------------------------------- caller-supplied identity is inert


async def test_a_body_supplied_actor_cannot_override_the_principal() -> None:
    """The vulnerability Phase 13 closes.

    Before this, an approval decision carried ``actor_user_id`` and the endpoint
    believed it — anyone reachable could approve as the finance director. Now
    the handler has no way to read an identity from the body at all.
    """
    token, plaintext = _live_token("operator")
    impersonated = uuid.uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=_app(token, EndpointAction.START_WORKFLOW)),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/protected",
            json={"actor_user_id": str(impersonated), "approved": True},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert response.status_code == 200
    assert response.json()["actor"] == str(token.user_id)
    assert response.json()["actor"] != str(impersonated)


async def test_the_approval_schema_rejects_a_supplied_actor_outright() -> None:
    """Not merely ignored — refused.

    A client still sending ``actor_user_id`` believes it is choosing the actor.
    Silently dropping the field would leave that belief intact.
    """
    from pydantic import ValidationError

    from custops.apps.api.schemas.approval import ApprovalDecisionRequest

    with pytest.raises(ValidationError):
        ApprovalDecisionRequest.model_validate(
            {"approved": True, "actor_user_id": str(uuid.uuid4())}
        )


async def test_the_approval_schema_still_accepts_a_plain_decision() -> None:
    """Forbidding extras must not have broken the legitimate shape."""
    from custops.apps.api.schemas.approval import ApprovalDecisionRequest

    parsed = ApprovalDecisionRequest.model_validate({"approved": False, "note": "Too risky."})

    assert parsed.approved is False
    assert parsed.note == "Too risky."


# ------------------------------------------------------------ hashing at rest


async def test_the_presented_token_is_matched_by_hash_not_by_value() -> None:
    """The store holds no plaintext, so the only possible lookup is by hash."""
    token, plaintext = _live_token("viewer")

    assert token.token_hash == hash_token(plaintext)
    assert plaintext not in token.token_hash
