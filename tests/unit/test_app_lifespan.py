"""Application startup and shutdown wiring.

Runnable without infrastructure by design: ``create_async_engine`` and
``Redis.from_url`` build objects without opening connections, so entering and
leaving the real lifespan exercises the actual resource wiring and disposal path
even on a machine with no PostgreSQL and no Redis. What is *not* proven here is
that either dependency answers — that is what the integration suite is for.
"""

from __future__ import annotations

from fastapi import FastAPI

from custops.apps.api.main import create_app
from custops.cache.redis_client import create_redis_client
from custops.config import Settings
from custops.db.engine import Database, create_database


async def test_lifespan_creates_and_disposes_resources(test_settings: Settings) -> None:
    application = create_app(settings=test_settings)

    assert not hasattr(application.state, "database")

    async with application.router.lifespan_context(application):
        assert isinstance(application.state.database, Database)
        assert application.state.redis is not None
        assert application.state.settings is test_settings
        pool_before_shutdown = application.state.database.engine.pool

    # dispose() replaces the connection pool, so a different pool object is the
    # observable evidence that shutdown actually released resources rather than
    # simply returning without error.
    assert application.state.database.engine.pool is not pool_before_shutdown


def test_engine_construction_does_not_require_a_live_server(test_settings: Settings) -> None:
    """The property that lets /health report a dependency failure at all.

    If the engine connected eagerly, an unreachable database would prevent the
    process from starting, and the endpoint whose job is to explain the outage
    would never be reachable.
    """
    database = create_database(test_settings)

    assert database.engine.url.host == "localhost"
    assert database.engine.url.database == "custops_test"
    # Credentials are masked in the URL's repr, which is what ends up in logs.
    assert "unit-test-password" not in repr(database.engine.url)


def test_redis_client_construction_does_not_require_a_live_server(
    test_settings: Settings,
) -> None:
    client = create_redis_client(test_settings)

    assert client.connection_pool.connection_kwargs["host"] == "localhost"
    assert client.connection_pool.connection_kwargs["port"] == 6379


def test_app_exposes_health_route(app: FastAPI) -> None:
    """Assert against the OpenAPI schema, not ``app.routes``.

    Current FastAPI keeps an included router as a single nested object in
    ``app.routes`` rather than flattening its paths, so scanning that list is
    both version-fragile and indirect. The generated schema is the contract
    callers actually see.
    """
    schema = app.openapi()

    assert "/health" in schema["paths"]
    assert "get" in schema["paths"]["/health"]
    # The degraded case is part of the published contract, not an undocumented
    # surprise for whoever wires up monitoring.
    assert "503" in schema["paths"]["/health"]["get"]["responses"]
