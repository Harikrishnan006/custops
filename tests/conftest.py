"""Shared fixtures.

Unit tests must run with no infrastructure at all, so settings are constructed
with explicit values and ``_env_file=None``: explicit init arguments outrank both
the environment and any local ``.env``, which keeps results identical on a
developer machine and in CI.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from custops.apps.api.main import create_app
from custops.config import LoggingSettings, PostgresSettings, RedisSettings, Settings

# Env var prefixes owned by this application; cleared when a test needs to
# observe defaults rather than the developer's environment.
SETTINGS_ENV_PREFIXES = ("CUSTOPS_", "POSTGRES_", "REDIS_", "LOG_")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove this application's env vars for the duration of a test."""
    for key in list(os.environ):
        if key.startswith(SETTINGS_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def test_settings() -> Settings:
    """Fully explicit settings for unit tests."""
    return Settings(
        _env_file=None,
        environment="test",
        debug=False,
        health_probe_timeout_seconds=0.5,
        postgres=PostgresSettings(
            _env_file=None,
            host="localhost",
            port=5432,
            user="custops",
            password=SecretStr("unit-test-password"),
            db="custops_test",
        ),
        redis=RedisSettings(_env_file=None, host="localhost", port=6379, db=0),
        logging=LoggingSettings(_env_file=None, level="INFO", format="json"),
    )


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    """An application instance built from explicit settings.

    The lifespan does not run under ``ASGITransport``, so ``app.state.database``
    and ``app.state.redis`` are absent here. Tests that need the endpoint but not
    the infrastructure override ``get_health_probes``; the lifespan itself is
    exercised in ``tests/unit/test_app_lifespan.py``.
    """
    return create_app(settings=test_settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client speaking directly to the ASGI app, no socket involved."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client
