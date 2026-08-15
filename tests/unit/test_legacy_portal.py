"""The legacy portal's shape and auth (§11).

Routes that need no database — login, logout, unauthenticated redirects — plus
the property that gives this whole phase its reason to exist: **there is no
API**. Account pages are exercised by the e2e suite against a real database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from custops.apps.legacy_portal.app import SESSION_COOKIE, SessionStore, create_portal
from custops.config import PortalSettings, Settings

PASSWORD = "portal-secret"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        portal=PortalSettings(
            _env_file=None,
            username="operator",
            password=SecretStr(PASSWORD),
        ),
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_portal(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://portal") as http:
        yield http


class TestNoApi:
    """D8: the portal has no API, and that is the point."""

    async def test_openapi_is_not_served(self, client: AsyncClient) -> None:
        """An OpenAPI document would advertise an API this app must not have."""
        assert (await client.get("/openapi.json")).status_code == 404

    async def test_interactive_docs_are_not_served(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 404

    async def test_the_login_page_is_html_not_json(self, client: AsyncClient) -> None:
        response = await client.get("/login")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<form" in response.text


class TestAuthentication:
    async def test_valid_credentials_set_a_session_cookie(self, client: AsyncClient) -> None:
        response = await client.post(
            "/login",
            data={"username": "operator", "password": PASSWORD},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/accounts"
        assert SESSION_COOKIE in response.cookies

    async def test_invalid_credentials_are_redirected_back(self, client: AsyncClient) -> None:
        """The portal redirects rather than returning a status code — legacy behaviour
        the Playwright driver has to detect by inspecting the URL."""
        response = await client.post(
            "/login",
            data={"username": "operator", "password": "wrong"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "/login" in response.headers["location"]
        assert SESSION_COOKIE not in response.cookies

    async def test_a_wrong_username_is_refused(self, client: AsyncClient) -> None:
        response = await client.post(
            "/login",
            data={"username": "intruder", "password": PASSWORD},
            follow_redirects=False,
        )

        assert "/login" in response.headers["location"]

    async def test_the_session_cookie_is_http_only(self, client: AsyncClient) -> None:
        """Script-readable session cookies are how a stored-XSS becomes account takeover."""
        response = await client.post(
            "/login",
            data={"username": "operator", "password": PASSWORD},
            follow_redirects=False,
        )

        assert "httponly" in response.headers["set-cookie"].lower()


class TestAuthorizationGates:
    async def test_the_account_list_requires_a_session(self, client: AsyncClient) -> None:
        response = await client.get("/accounts", follow_redirects=False)

        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    async def test_the_root_requires_a_session(self, client: AsyncClient) -> None:
        response = await client.get("/", follow_redirects=False)

        assert "/login" in response.headers["location"]

    async def test_submitting_a_tier_change_requires_a_session(self, client: AsyncClient) -> None:
        """The mutation itself must be gated, not merely the page that offers it."""
        response = await client.post(
            "/accounts/11111111-1111-1111-1111-111111111111/tier",
            data={"tier": "enterprise", "seats": "5"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    async def test_a_forged_cookie_is_refused(self, settings: Settings) -> None:
        app = create_portal(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://portal",
            cookies={SESSION_COOKIE: "not-a-real-token"},
        ) as forged:
            response = await forged.get("/accounts", follow_redirects=False)

        assert "/login" in response.headers["location"]


class TestSessionStore:
    def test_an_issued_token_is_valid(self) -> None:
        store = SessionStore()

        assert store.is_valid(store.issue())

    def test_an_unissued_token_is_not(self) -> None:
        assert not SessionStore().is_valid("invented")

    def test_none_is_not_valid(self) -> None:
        assert not SessionStore().is_valid(None)

    def test_revoking_invalidates(self) -> None:
        store = SessionStore()
        token = store.issue()

        store.revoke(token)

        assert not store.is_valid(token)

    def test_tokens_are_unpredictable(self) -> None:
        store = SessionStore()

        assert len({store.issue() for _ in range(20)}) == 20
