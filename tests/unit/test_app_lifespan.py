"""Application startup and shutdown wiring.

Runnable without infrastructure by design: ``create_async_engine`` and
``Redis.from_url`` build objects without opening connections, so entering and
leaving the real lifespan exercises the actual resource wiring and disposal path
even on a machine with no PostgreSQL and no Redis. What is *not* proven here is
that either dependency answers — that is what the integration suite is for.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from fastapi import FastAPI

from custops.apps.api import main
from custops.apps.api.main import create_app
from custops.apps.orchestrator import checkpointer
from custops.cache.redis_client import create_redis_client
from custops.config import Settings
from custops.db.engine import Database, create_database


@pytest.fixture
def without_checkpointer_setup(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    """Record the startup checkpointer setup instead of performing it.

    The lifespan creates the checkpointer's tables, which is the one thing in it
    that genuinely reaches PostgreSQL. Stubbing it keeps this module runnable
    with no infrastructure — its subject is resource wiring and disposal — while
    the recorded calls still let the tests below assert *that* startup does it.
    The real DDL is exercised by the integration suite.
    """
    calls: list[Settings] = []

    async def _record(settings: Settings) -> None:
        calls.append(settings)

    monkeypatch.setattr(main, "ensure_checkpointer_ready", _record)
    return calls


async def test_lifespan_creates_and_disposes_resources(
    test_settings: Settings, without_checkpointer_setup: list[Settings]
) -> None:
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


class TestCheckpointerSchemaIsAStartupConcern:
    """Where the checkpointer's DDL runs, and where it must never run again.

    `setup()` issues `CREATE INDEX CONCURRENTLY`, which waits for every open
    transaction that can see the table. Inside a request there is always one —
    the authentication dependency's own session — so the request waited on a
    transaction that could not finish until the request did. It survived review
    because `IF NOT EXISTS` makes the statement a no-op once the indexes exist,
    so only the first runs against a fresh database ever hung.

    These tests pin both halves of the fix: startup does it, and the per-run
    path does not.
    """

    async def test_startup_prepares_the_checkpointer_exactly_once(
        self, test_settings: Settings, without_checkpointer_setup: list[Settings]
    ) -> None:
        application = create_app(settings=test_settings)

        async with application.router.lifespan_context(application):
            pass

        assert without_checkpointer_setup == [test_settings]

    async def test_each_startup_prepares_its_own_process(
        self, test_settings: Settings, without_checkpointer_setup: list[Settings]
    ) -> None:
        """Once per process, not once per import: a second app prepares again."""
        for _ in range(2):
            application = create_app(settings=test_settings)
            async with application.router.lifespan_context(application):
                pass

        assert len(without_checkpointer_setup) == 2

    def test_opening_a_checkpointer_does_not_run_setup(self) -> None:
        """The regression itself.

        Inspecting the function is deliberate: calling `open_checkpointer` for
        real needs PostgreSQL, and a stubbed saver would only prove the stub
        was not called. What must stay true is that the per-run path contains
        no setup call at all.

        Parsed rather than grepped, so that a comment mentioning `setup()` —
        and there is one, explaining why it is absent — cannot fail the test,
        and so that `await x.setup()` cannot pass it by being spelled
        differently.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(checkpointer.open_checkpointer)))

        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert "setup" not in called

    def test_startup_is_the_thing_that_runs_setup(self) -> None:
        """The other half: the call did not simply disappear."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(checkpointer.ensure_checkpointer_ready)))

        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert "setup" in called

    def test_startup_failure_is_not_swallowed(self) -> None:
        """A checkpointer without tables cannot persist a paused workflow (§7).

        Booting anyway would defer that discovery to the first approval pause,
        which is the worst possible moment to find out.
        """
        source = inspect.getsource(main.lifespan)

        assert "ensure_checkpointer_ready" in source
        # No try/except wrapping it, and no bare continue-on-error.
        assert "except" not in source.split("ensure_checkpointer_ready")[1].split("yield")[0]
