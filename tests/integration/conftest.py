"""Integration fixtures.

These tests target the PostgreSQL and Redis instances named by the *real*
configuration (``.env`` / environment), not the synthetic unit-test settings —
the point is to exercise the deployment a developer actually has running.

When a dependency is unusable the tests skip rather than fail, and the skip
reason says exactly what is missing.

**Reachability is not usability.** An earlier version of this guard decided
availability with a plain TCP connect. That is wrong in a way that matters: a
PostgreSQL that is listening but has no ``custops`` role un-skips every
integration test, and all of them then fail on authentication — 64 errors that
look like code defects and are not. The probe below therefore opens a real
connection with the configured credentials and checks that pgvector is
available, because migration 0001 cannot run without it.

The probe runs once, at collection time, with a short timeout so it cannot hang
the suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from custops.apps.api.main import create_app
from custops.config import Settings, get_settings
from custops.db.engine import Database, create_database
from tests.support import service_reachable

_settings = get_settings()

PROBE_TIMEOUT_SECONDS = 5.0


def _probe_postgres() -> str | None:
    """Return None when PostgreSQL is usable, else why it is not.

    Checks three things in order, so the reason names the first real blocker
    rather than a downstream symptom: the port answers, the configured
    credentials authenticate against the configured database, and the pgvector
    extension is available to be created.
    """
    host = _settings.postgres.host
    port = _settings.postgres.port

    if not service_reachable(host, port):
        return f"nothing listening at {host}:{port}"

    async def check() -> str | None:
        import asyncpg

        try:
            connection = await asyncpg.connect(
                host=host,
                port=port,
                user=_settings.postgres.user,
                password=_settings.postgres.password.get_secret_value(),
                database=_settings.postgres.db,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # any connect failure means "not usable"
            return f"{host}:{port} is listening but unusable: {type(error).__name__}: {error}"

        try:
            available = await connection.fetchval(
                "select count(*) from pg_available_extensions where name = 'vector'"
            )
            if not available:
                return (
                    "pgvector is not available on this server; migration 0001 "
                    "cannot run (see docs/INTEGRATION-VERIFICATION.md §3.2)"
                )
        finally:
            await connection.close()
        return None

    try:
        return asyncio.run(asyncio.wait_for(check(), timeout=PROBE_TIMEOUT_SECONDS * 2))
    except Exception as error:  # a probe that fails is a probe that says so
        return f"probe failed: {type(error).__name__}: {error}"


_postgres_unavailable_reason = _probe_postgres()
postgres_available = _postgres_unavailable_reason is None
redis_available = service_reachable(_settings.redis.host, _settings.redis.port)

requires_postgres = pytest.mark.skipif(
    not postgres_available,
    reason=(
        f"PostgreSQL unusable — {_postgres_unavailable_reason}. "
        "Setup: docs/INTEGRATION-VERIFICATION.md"
    ),
)
requires_redis = pytest.mark.skipif(
    not redis_available,
    reason=(
        f"Redis not reachable at {_settings.redis.host}:{_settings.redis.port} — "
        "see README 'Running the dependencies'"
    ),
)


@pytest.fixture
def runtime_settings() -> Settings:
    return _settings


@pytest.fixture
async def database(runtime_settings: Settings) -> AsyncIterator[Database]:
    db = create_database(runtime_settings)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def live_app(runtime_settings: Settings) -> AsyncIterator[FastAPI]:
    """An application with its lifespan actually run, holding real connections."""
    application = create_app(settings=runtime_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=live_app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client
