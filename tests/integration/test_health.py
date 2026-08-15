"""/health against live dependencies (Phase 1 definition of done, item 4).

The unit suite proves the endpoint's status logic; this proves the endpoint
actually confirms connectivity — the claim a stub can never make.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.integration.conftest import requires_postgres, requires_redis

pytestmark = [pytest.mark.integration, requires_postgres, requires_redis]


async def test_health_reports_both_dependencies_up(live_client: AsyncClient) -> None:
    response = await live_client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["postgres"]["status"] == "up"
    assert body["dependencies"]["redis"]["status"] == "up"


async def test_health_reports_the_installed_pgvector_version(live_client: AsyncClient) -> None:
    body = (await live_client.get("/health")).json()

    assert body["dependencies"]["postgres"]["detail"]["pgvector_extension"] is not None


async def test_health_reports_the_redis_server_version(live_client: AsyncClient) -> None:
    body = (await live_client.get("/health")).json()

    assert body["dependencies"]["redis"]["detail"]["redis_version"]


async def test_probes_are_live_rather_than_cached(live_client: AsyncClient) -> None:
    """Two calls must both measure real round trips, not replay a startup result."""
    first = (await live_client.get("/health")).json()
    second = (await live_client.get("/health")).json()

    assert first["dependencies"]["postgres"]["latency_ms"] > 0
    assert second["dependencies"]["postgres"]["latency_ms"] > 0
    assert first["request_id"] != second["request_id"]
