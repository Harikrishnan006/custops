"""The legacy portal's HTTP surface — HTML forms only, no JSON.

Every route returns rendered HTML or a redirect. There is no endpoint that
accepts or returns JSON, and adding one would quietly remove the reason
Playwright exists in this project.

Session handling is a signed cookie over an in-process token set: crude, and
authentic for the kind of system this simulates. A restart invalidates sessions,
which is also authentic.

**Escaping.** Every interpolated value goes through ``html.escape``. Account
names come from the database and a legacy app that reflects them unescaped is a
stored-XSS hole; that this one is simulated is not a reason to write the bug.
"""

from __future__ import annotations

import html
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.config import Settings, get_settings
from custops.db.engine import Database, create_database
from custops.domain.enums import EntitlementStatus
from custops.domain.models.customer import Account
from custops.domain.models.entitlement import Entitlement
from custops.observability.logging import get_logger

logger = get_logger(__name__)

SESSION_COOKIE = "portal_session"

# Tiers this portal can provision. Deliberately its own list rather than a read
# of the billing plan catalogue: a legacy provisioning system knows nothing
# about billing's plans, and pretending otherwise would hide the integration
# problem the Validator exists to catch.
PROVISIONABLE_TIERS = ("starter", "professional", "enterprise")

_STYLE = """
body { font-family: Georgia, serif; margin: 2rem; background: #f5f2ec; color: #2b2b2b; }
table { border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #b9b2a5; padding: 0.4rem 0.8rem; text-align: left; }
.banner { background: #dfe9d8; border: 1px solid #7d9b6a; padding: 0.6rem; margin: 1rem 0; }
.error { background: #f0d9d9; border: 1px solid #a35c5c; padding: 0.6rem; margin: 1rem 0; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{html.escape(title)}</title>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    )


class SessionStore:
    """In-process session tokens.

    A real legacy portal would use a database or a file. In-process is enough to
    make the login step genuine — Playwright must actually submit the form and
    carry the cookie — without inventing infrastructure this project does not
    otherwise need.
    """

    def __init__(self) -> None:
        self._tokens: set[str] = set()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def is_valid(self, token: str | None) -> bool:
        return bool(token) and token in self._tokens

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.discard(token)


def create_portal(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    """Build the portal application."""
    resolved = settings if settings is not None else get_settings()
    db = database if database is not None else create_database(resolved)
    sessions = SessionStore()

    app = FastAPI(
        title="Northwind Provisioning Console",
        description="Legacy entitlement provisioning. No API.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # there is no API to document
    )
    app.state.sessions = sessions

    def _authenticated(token: str | None) -> bool:
        return sessions.is_valid(token)

    def _login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def index(
        portal_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        if not _authenticated(portal_session):
            return _login_redirect()
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(error: str | None = None) -> HTMLResponse:
        banner = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return _page(
            "Sign in",
            f"""
            <h1>Northwind Provisioning Console</h1>
            {banner}
            <form method="post" action="/login">
              <p><label>Operator <input name="username" id="username"></label></p>
              <p><label>Password
                 <input name="password" id="password" type="password"></label></p>
              <p><button type="submit" id="signin">Sign in</button></p>
            </form>
            """,
        )

    @app.post("/login")
    async def login(
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        expected_user = resolved.portal.username
        expected_password = resolved.portal.password.get_secret_value()

        # Constant-time comparison. The credential is synthetic, but writing the
        # timing leak in and calling it simulation teaches the wrong pattern.
        ok = secrets.compare_digest(username, expected_user) and secrets.compare_digest(
            password, expected_password
        )
        if not ok:
            logger.warning("portal_login_failed", username=username)
            return RedirectResponse("/login?error=Invalid+credentials", status_code=303)

        token = sessions.issue()
        response = RedirectResponse("/accounts", status_code=303)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/logout")
    async def logout(
        portal_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        sessions.revoke(portal_session)
        response = _login_redirect()
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/accounts", response_class=HTMLResponse)
    async def list_accounts(
        request: Request,
        q: str = "",
        portal_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        if not _authenticated(portal_session):
            return _login_redirect()

        async with db.session_factory() as session:
            rows = await _search_accounts(session, q)

        listing = "".join(
            f"<tr><td>{html.escape(name)}</td>"
            f'<td><a href="/accounts/{account_id}" id="acct-{account_id}">'
            f"{html.escape(str(account_id))}</a></td>"
            f"<td>{html.escape(tier or 'unprovisioned')}</td></tr>"
            for account_id, name, tier in rows
        )
        return _page(
            "Accounts",
            f"""
            <h1>Accounts</h1>
            <form method="get" action="/accounts">
              <input name="q" id="q" value="{html.escape(q)}" placeholder="Search">
              <button type="submit">Search</button>
            </form>
            <table id="accounts">
              <tr><th>Name</th><th>Account</th><th>Tier</th></tr>
              {listing}
            </table>
            """,
        )

    @app.get("/accounts/{account_id}", response_class=HTMLResponse)
    async def account_detail(
        account_id: uuid.UUID,
        updated: str | None = None,
        error: str | None = None,
        portal_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        if not _authenticated(portal_session):
            return _login_redirect()

        async with db.session_factory() as session:
            account = await session.get(Account, account_id)
            if account is None:
                return _page("Not found", "<h1>No such account</h1>")
            entitlement = await _entitlement_for(session, account_id)

        tier = entitlement.tier if entitlement else ""
        seats = entitlement.seats if entitlement else 1
        synced = (
            entitlement.last_synced_at.isoformat()
            if entitlement and entitlement.last_synced_at
            else "never"
        )
        options = "".join(
            f'<option value="{t}"{" selected" if t == tier else ""}>{t}</option>'
            for t in PROVISIONABLE_TIERS
        )
        banner = (
            f'<p class="banner" id="confirmation">{html.escape(updated)}</p>' if updated else ""
        )
        problem = f'<p class="error" id="error">{html.escape(error)}</p>' if error else ""

        return _page(
            f"Account {account.name}",
            f"""
            <h1>{html.escape(account.name)}</h1>
            {banner}{problem}
            <table>
              <tr><th>Account</th><td id="account-id">{html.escape(str(account_id))}</td></tr>
              <tr><th>Provisioned tier</th><td id="current-tier">
                  {html.escape(tier or "unprovisioned")}</td></tr>
              <tr><th>Seats</th><td id="seats">{seats}</td></tr>
              <tr><th>Last synchronised</th><td id="last-synced">{html.escape(synced)}</td></tr>
            </table>
            <h2>Change tier</h2>
            <form method="post" action="/accounts/{account_id}/tier">
              <p><label>Tier <select name="tier" id="tier">{options}</select></label></p>
              <p><label>Seats
                 <input name="seats" id="seats-input" type="number" value="{seats}"></label></p>
              <p><button type="submit" id="apply">Apply change</button></p>
            </form>
            <p><a href="/accounts">Back</a></p>
            """,
        )

    @app.post("/accounts/{account_id}/tier")
    async def change_tier(
        account_id: uuid.UUID,
        tier: Annotated[str, Form()],
        seats: Annotated[int, Form()] = 1,
        portal_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        if not _authenticated(portal_session):
            return _login_redirect()

        if tier not in PROVISIONABLE_TIERS:
            return RedirectResponse(f"/accounts/{account_id}?error=Unknown+tier", status_code=303)

        async with db.session_factory() as session:
            account = await session.get(Account, account_id)
            if account is None:
                return _page("Not found", "<h1>No such account</h1>")

            entitlement = await _entitlement_for(session, account_id)
            now = datetime.now(UTC)
            if entitlement is None:
                entitlement = Entitlement(
                    id=uuid.uuid4(),
                    account_id=account_id,
                    tier=tier,
                    seats=seats,
                    status=EntitlementStatus.PROVISIONED,
                    last_synced_at=now,
                )
                session.add(entitlement)
            else:
                entitlement.tier = tier
                entitlement.seats = seats
                entitlement.status = EntitlementStatus.PROVISIONED
                entitlement.last_synced_at = now
            await session.commit()

        logger.info("portal_tier_changed", account_id=str(account_id), tier=tier)
        return RedirectResponse(
            f"/accounts/{account_id}?updated=Provisioned+{tier}", status_code=303
        )

    return app


async def _entitlement_for(session: AsyncSession, account_id: uuid.UUID) -> Entitlement | None:
    return (
        await session.execute(select(Entitlement).where(Entitlement.account_id == account_id))
    ).scalar_one_or_none()


async def _search_accounts(
    session: AsyncSession, query: str
) -> list[tuple[uuid.UUID, str, str | None]]:
    statement = (
        select(Account.id, Account.name, Entitlement.tier)
        .outerjoin(Entitlement, Entitlement.account_id == Account.id)
        .order_by(Account.name)
        .limit(50)
    )
    if query:
        statement = statement.where(Account.name.ilike(f"%{query}%"))
    rows = (await session.execute(statement)).all()
    return [(row[0], row[1], row[2]) for row in rows]


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_portal(settings), host=settings.portal.host, port=settings.portal.port)
