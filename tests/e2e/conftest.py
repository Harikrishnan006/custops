"""End-to-end fixtures: a real portal, a real browser.

Two things must be present, and each is reported separately so a skip says which
one is missing:

* **PostgreSQL** — the portal reads and writes real entitlements.
* **Chromium** — installed with ``uv run playwright install chromium``. The
  package alone is not enough; the browser binary is a separate download, which
  is why this is probed rather than assumed.

The portal runs in-process on a real socket. Playwright drives a real browser
against a real HTTP server — an ASGI transport would skip the parts (redirects,
cookies, form encoding, navigation) that this phase exists to prove.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator

import pytest
import uvicorn

from custops.apps.legacy_portal.app import create_portal
from custops.config import PortalSettings, Settings, get_settings
from custops.db.engine import create_database

PORTAL_USER = "provisioning.operator"
PORTAL_PASSWORD = "portal-e2e-secret"


def _chromium_available() -> str | None:
    """Return None when Chromium can launch, else why it cannot."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - dependency guard
        return "the 'playwright' package is not installed"

    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            browser.close()
    except Exception as error:
        return (
            f"Chromium could not launch ({type(error).__name__}). "
            "Install it with: uv run playwright install chromium"
        )
    return None


_browser_problem = _chromium_available()

requires_browser = pytest.mark.skipif(
    _browser_problem is not None,
    reason=f"Browser unavailable — {_browser_problem}",
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def portal_settings() -> Settings:
    """Real database settings, with the portal on an ephemeral port."""
    base = get_settings()
    port = _free_port()
    return Settings(
        _env_file=None,
        environment=base.environment,
        postgres=base.postgres,
        redis=base.redis,
        providers=base.providers,
        portal=PortalSettings(
            _env_file=None,
            host="127.0.0.1",
            port=port,
            base_url=f"http://127.0.0.1:{port}",
            username=PORTAL_USER,
            password=PORTAL_PASSWORD,
            headless=True,
        ),
    )


@pytest.fixture
async def running_portal(portal_settings: Settings) -> AsyncIterator[Settings]:
    """The portal, served over a real socket for the duration of a test."""
    database = create_database(portal_settings)
    app = create_portal(portal_settings, database=database)

    config = uvicorn.Config(
        app,
        host=portal_settings.portal.host,
        port=portal_settings.portal.port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    # Wait for the socket rather than sleeping a guessed interval.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - startup failure
        raise RuntimeError("The portal did not start.")

    try:
        yield portal_settings
    finally:
        server.should_exit = True
        await task
        await database.dispose()


@pytest.fixture(scope="session")
def anyio_backend() -> Iterator[str]:  # pragma: no cover - plugin plumbing
    yield "asyncio"
