"""Integration fixtures.

These tests target the PostgreSQL and Redis instances named by the *real*
configuration (``.env`` / environment), not the synthetic unit-test settings —
the point is to exercise the deployment a developer actually has running.

When a dependency is unreachable the tests skip rather than fail. Reachability is
decided once, at collection time, with a plain TCP connect: cheap, and it cannot
itself hang the suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from custops.apps.api.main import create_app
from custops.config import Settings, get_settings
from custops.db.engine import Database, create_database
from tests.support import service_reachable

_settings = get_settings()

postgres_available = service_reachable(_settings.postgres.host, _settings.postgres.port)
redis_available = service_reachable(_settings.redis.host, _settings.redis.port)

requires_postgres = pytest.mark.skipif(
    not postgres_available,
    reason=(
        f"PostgreSQL not reachable at {_settings.postgres.host}:{_settings.postgres.port} — "
        "see README 'Running the dependencies'"
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
