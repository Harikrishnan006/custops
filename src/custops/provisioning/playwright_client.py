"""The real portal driver (§11).

Browser startup → login → navigate → locate the account → submit the tier form
→ extract the confirmation → verify. Written against the installed Playwright
1.62 async API rather than recalled.

Two properties make this testable rather than flaky:

**Selectors are ids, not text.** The portal renders stable ids (``#tier``,
``#apply``, ``#current-tier``) precisely so automation does not depend on
wording. Text-matching selectors are how browser suites become brittle, and a
legacy portal whose copy nobody controls is exactly where that bites.

**The confirmation is read back, not assumed.** ``set_tier`` re-reads the
rendered tier after submitting and reports what the portal *says*, not what was
asked for. A form that accepts a submission and provisions something else is a
real failure mode, and echoing the request would hide it — the same mistake as
trusting a 200 from the billing API.

Playwright needs browser binaries, installed separately with
``uv run playwright install chromium``. Their absence raises
``BROWSER_UNAVAILABLE`` rather than a raw import or launch error, so a missing
browser is a legible failure rather than a stack trace inside a workflow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from custops.config import PortalSettings
from custops.observability.logging import get_logger
from custops.provisioning.client import (
    ProvisioningError,
    ProvisioningErrorCode,
    ProvisioningResult,
)

logger = get_logger(__name__)


class PlaywrightProvisioningClient:
    """Drives the legacy portal with a real browser."""

    def __init__(self, settings: PortalSettings) -> None:
        self._settings = settings

    @asynccontextmanager
    async def _page(self) -> AsyncIterator[Any]:
        """A logged-in page, torn down afterwards.

        One browser per operation. Slower than pooling, and correct: a workflow
        that pauses for approval between operations cannot hold a browser open,
        and a leaked browser process outlives the run that created it.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:  # pragma: no cover - dependency guard
            raise ProvisioningError(
                ProvisioningErrorCode.BROWSER_UNAVAILABLE,
                "The 'playwright' package is not installed.",
            ) from error

        async with async_playwright() as driver:
            try:
                browser = await driver.chromium.launch(headless=self._settings.headless)
            except Exception as error:
                raise ProvisioningError(
                    ProvisioningErrorCode.BROWSER_UNAVAILABLE,
                    "Could not launch Chromium. Install browsers with "
                    "'uv run playwright install chromium'.",
                ) from error

            try:
                context = await browser.new_context(base_url=self._settings.base_url)
                context.set_default_timeout(self._settings.timeout_ms)
                page = await context.new_page()
                await self._login(page)
                yield page
            finally:
                await browser.close()

    async def _login(self, page: Any) -> None:
        """Sign in through the form. There is no API to authenticate against."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            await page.goto("/login")
            await page.fill("#username", self._settings.username)
            await page.fill("#password", self._settings.password.get_secret_value())
            await page.click("#signin")
            await page.wait_for_url("**/accounts", timeout=self._settings.timeout_ms)
        except PlaywrightTimeout as error:
            # Rejected credentials also land here, and that is the whole
            # subtlety. The portal answers a bad sign-in by redirecting back to
            # /login rather than returning a status, so waiting for /accounts
            # never matches and expires. Reporting that as a timeout would
            # blame the network for the portal's answer — and left the
            # `login_failed` branch below unreachable, which is what this
            # ordering fixes. Read the URL before deciding which failure it was.
            if "/login" in page.url:
                raise ProvisioningError(
                    ProvisioningErrorCode.LOGIN_FAILED,
                    "The portal rejected the provisioning credentials.",
                ) from error
            raise ProvisioningError(
                ProvisioningErrorCode.TIMEOUT, "Timed out signing in to the portal."
            ) from error

        # Kept for the case where the wait resolves on /login rather than
        # expiring: same conclusion, reached without an exception.
        if "/login" in page.url:
            raise ProvisioningError(
                ProvisioningErrorCode.LOGIN_FAILED,
                "The portal rejected the provisioning credentials.",
            )

    async def set_tier(self, *, account_id: str, tier: str, seats: int) -> ProvisioningResult:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        async with self._page() as page:
            try:
                await page.goto(f"/accounts/{account_id}")

                if await page.locator("#current-tier").count() == 0:
                    raise ProvisioningError(
                        ProvisioningErrorCode.ACCOUNT_NOT_FOUND,
                        f"The portal has no account {account_id}.",
                    )

                await page.select_option("#tier", tier)
                await page.fill("#seats-input", str(seats))
                await page.click("#apply")
                await page.wait_for_url(f"**/accounts/{account_id}?*")

                confirmation = await page.text_content("#confirmation")
                if not confirmation:
                    raise ProvisioningError(
                        ProvisioningErrorCode.CONFIRMATION_MISSING,
                        "The portal did not confirm the change.",
                    )

                # What the portal says it did, read back from the page.
                confirmed = (await page.text_content("#current-tier") or "").strip()
                seats_text = (await page.text_content("#seats") or "").strip()

            except PlaywrightTimeout as error:
                raise ProvisioningError(
                    ProvisioningErrorCode.TIMEOUT,
                    f"Timed out provisioning {tier} for {account_id}.",
                ) from error

        logger.info(
            "portal_provisioned",
            account_id=account_id,
            requested=tier,
            confirmed=confirmed,
        )
        return ProvisioningResult(
            account_id=account_id,
            requested_tier=tier,
            confirmed_tier=confirmed,
            seats=int(seats_text) if seats_text.isdigit() else seats,
            confirmation_text=confirmation.strip(),
        )

    async def read_tier(self, *, account_id: str) -> str | None:
        """Re-read the provisioned tier from the portal.

        Used by the Validator. Deliberately a browser read of the portal's own
        page rather than a query against the entitlements table: checking our
        own copy would validate the wrong side of the integration.
        """
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        async with self._page() as page:
            try:
                await page.goto(f"/accounts/{account_id}")
                if await page.locator("#current-tier").count() == 0:
                    return None
                tier = (await page.text_content("#current-tier") or "").strip()
            except PlaywrightTimeout as error:
                raise ProvisioningError(
                    ProvisioningErrorCode.TIMEOUT,
                    f"Timed out reading the tier for {account_id}.",
                ) from error

        return tier or None
