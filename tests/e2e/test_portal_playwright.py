"""Driving the legacy portal with a real browser (§11).

The full sequence the spec names: browser startup → login → navigate → locate
the account → submit the tier change form → extract confirmation → verify. No
ASGI shortcut — a real HTTP server, real redirects, real cookies, real form
encoding, because those are the parts that break.

Pending unless both PostgreSQL and Chromium are available; the skip reason says
which is missing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from tests.e2e.conftest import requires_browser
from tests.integration.conftest import requires_postgres

from custops.config import Settings
from custops.db.engine import Database, create_database
from custops.domain.seed import clear_seed_data, seed_all, seed_id
from custops.provisioning.client import ProvisioningError, ProvisioningErrorCode
from custops.provisioning.playwright_client import PlaywrightProvisioningClient

pytestmark = [pytest.mark.integration, requires_postgres, requires_browser]

NOW = datetime.now(UTC)
ACME = seed_id("account", "acme")


@pytest.fixture
async def seeded(portal_settings: Settings) -> AsyncIterator[Database]:
    database = create_database(portal_settings)
    async with database.session_factory() as session:
        await seed_all(session, now=NOW)
        await session.commit()
    try:
        yield database
    finally:
        async with database.session_factory() as session:
            await clear_seed_data(session)
            await session.commit()
        await database.dispose()


class TestBrowserProvisioning:
    async def test_the_full_sequence_provisions_a_tier(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        """Startup → login → navigate → submit → confirm → verify."""
        client = PlaywrightProvisioningClient(running_portal.portal)

        result = await client.set_tier(account_id=str(ACME), tier="enterprise", seats=20)

        assert result.confirmed_tier == "enterprise"
        assert result.matches_request
        assert "enterprise" in result.confirmation_text.lower()

    async def test_the_change_is_readable_back_through_the_portal(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        """§14's re-read: the Validator's view of provisioning, via the portal."""
        client = PlaywrightProvisioningClient(running_portal.portal)
        await client.set_tier(account_id=str(ACME), tier="enterprise", seats=20)

        assert await client.read_tier(account_id=str(ACME)) == "enterprise"

    async def test_seats_are_carried_through_the_form(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        client = PlaywrightProvisioningClient(running_portal.portal)

        result = await client.set_tier(account_id=str(ACME), tier="enterprise", seats=42)

        assert result.seats == 42

    async def test_the_change_actually_reaches_the_database(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        """A browser that reports success without persisting is the failure this catches."""
        from sqlalchemy import select

        from custops.domain.models.entitlement import Entitlement

        client = PlaywrightProvisioningClient(running_portal.portal)
        await client.set_tier(account_id=str(ACME), tier="enterprise", seats=20)

        async with seeded.session_factory() as session:
            entitlement = (
                await session.execute(select(Entitlement).where(Entitlement.account_id == ACME))
            ).scalar_one()

        assert entitlement.tier == "enterprise"
        assert entitlement.last_synced_at is not None


class TestBrowserFailures:
    async def test_bad_credentials_raise_login_failed(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        """The portal redirects instead of returning a status; the driver notices."""
        from custops.config import PortalSettings

        wrong = PortalSettings(
            _env_file=None,
            base_url=running_portal.portal.base_url,
            username=running_portal.portal.username,
            password="definitely-not-the-password",
            headless=True,
        )
        client = PlaywrightProvisioningClient(wrong)

        with pytest.raises(ProvisioningError) as error:
            await client.set_tier(account_id=str(ACME), tier="enterprise", seats=1)

        assert error.value.code == ProvisioningErrorCode.LOGIN_FAILED

    async def test_an_unknown_account_raises_not_found(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        client = PlaywrightProvisioningClient(running_portal.portal)

        with pytest.raises(ProvisioningError) as error:
            await client.set_tier(account_id=str(uuid.uuid4()), tier="enterprise", seats=1)

        assert error.value.code == ProvisioningErrorCode.ACCOUNT_NOT_FOUND

    async def test_reading_an_unknown_account_returns_none(
        self, running_portal: Settings, seeded: Database
    ) -> None:
        """Not provisioned is not the same as disagreeing, and must not raise."""
        client = PlaywrightProvisioningClient(running_portal.portal)

        assert await client.read_tier(account_id=str(uuid.uuid4())) is None
