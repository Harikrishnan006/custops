"""FastAPI dependency providers.

Long-lived resources (engine, Redis client) are created once in the lifespan and
read from ``app.state``; these providers expose them to handlers. Handlers never
construct connections themselves.

``HealthProbes`` is a Protocol rather than a concrete call because it is the seam
that makes the health contract testable without infrastructure: the endpoint's
status logic — which dependency states produce 200 versus 503, and what the body
looks like — is verified against stub probes, while the live implementation is
verified separately against real services. Without this seam, no part of
``/health`` could be tested until PostgreSQL and Redis were both running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request
from redis.asyncio import Redis

from custops.cache.redis_client import probe_redis
from custops.config import Settings
from custops.db.engine import Database, probe_postgres
from custops.observability.probes import ProbeResult


def get_settings_from_state(request: Request) -> Settings:
    """Return the settings bound to this application instance."""
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    """Return the process-wide database handle created during startup."""
    database: Database = request.app.state.database
    return database


def get_redis(request: Request) -> Redis:
    """Return the process-wide Redis client created during startup."""
    client: Redis = request.app.state.redis
    return client


class HealthProbes(Protocol):
    """The set of dependency probes ``/health`` reports on."""

    async def postgres(self) -> ProbeResult: ...

    async def redis(self) -> ProbeResult: ...


@dataclass(frozen=True, slots=True)
class LiveHealthProbes:
    """Probes that talk to the real dependencies."""

    database: Database
    redis_client: Redis
    timeout_seconds: float

    async def postgres(self) -> ProbeResult:
        return await probe_postgres(self.database.engine, timeout=self.timeout_seconds)

    async def redis(self) -> ProbeResult:
        return await probe_redis(self.redis_client, timeout=self.timeout_seconds)


def get_health_probes(request: Request) -> HealthProbes:
    """Provide live probes. Overridden in tests via ``dependency_overrides``."""
    settings = get_settings_from_state(request)
    return LiveHealthProbes(
        database=get_database(request),
        redis_client=get_redis(request),
        timeout_seconds=settings.health_probe_timeout_seconds,
    )
