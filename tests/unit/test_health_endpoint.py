"""/health contract (Phase 1 definition of done, item 4) — without infrastructure.

These tests pin the *logic*: which dependency states produce 200 versus 503, and
what the body says. They use stub probes, so they prove nothing about real
connectivity — that claim belongs to ``tests/integration/test_health.py``, which
runs the same endpoint against live services. Keeping the two apart keeps each
one's claim honest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from custops.apps.api.dependencies import get_health_probes
from custops.config import Settings
from custops.observability.logging import configure_logging
from custops.observability.probes import ProbeResult, ProbeStatus

POSTGRES_UP = ProbeResult(
    name="postgres",
    status=ProbeStatus.UP,
    latency_ms=1.5,
    detail={"server_version": "17.2", "pgvector_extension": "0.8.6"},
)
REDIS_UP = ProbeResult(name="redis", status=ProbeStatus.UP, latency_ms=0.4, detail={})
REDIS_DOWN = ProbeResult(
    name="redis",
    status=ProbeStatus.DOWN,
    latency_ms=500.0,
    error="ConnectionError: Error 111 connecting to localhost:6379",
)
POSTGRES_DOWN = ProbeResult(
    name="postgres",
    status=ProbeStatus.DOWN,
    latency_ms=2000.0,
    error="probe exceeded 2s timeout",
)


@dataclass(frozen=True)
class StubProbes:
    postgres_result: ProbeResult
    redis_result: ProbeResult

    async def postgres(self) -> ProbeResult:
        return self.postgres_result

    async def redis(self) -> ProbeResult:
        return self.redis_result


def _override(app: FastAPI, postgres: ProbeResult, redis: ProbeResult) -> None:
    app.dependency_overrides[get_health_probes] = lambda: StubProbes(postgres, redis)


async def test_all_dependencies_up_returns_200(app: FastAPI, client: AsyncClient) -> None:
    _override(app, POSTGRES_UP, REDIS_UP)

    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert set(body["dependencies"]) == {"postgres", "redis"}
    assert body["dependencies"]["postgres"]["status"] == "up"
    assert body["dependencies"]["postgres"]["detail"]["pgvector_extension"] == "0.8.6"
    assert body["dependencies"]["redis"]["status"] == "up"


async def test_execution_id_is_present_but_null(app: FastAPI, client: AsyncClient) -> None:
    # Phase 1 has no workflow runtime; the field exists so the contract does not
    # change shape when Phase 5 starts populating it.
    _override(app, POSTGRES_UP, REDIS_UP)

    body = (await client.get("/health")).json()

    assert "execution_id" in body
    assert body["execution_id"] is None


async def test_redis_down_returns_503_and_says_why(app: FastAPI, client: AsyncClient) -> None:
    _override(app, POSTGRES_UP, REDIS_DOWN)

    response = await client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    # Same schema as the healthy response: a caller does not need to parse two
    # shapes to find out what broke.
    assert body["dependencies"]["postgres"]["status"] == "up"
    assert body["dependencies"]["redis"]["status"] == "down"
    assert "6379" in body["dependencies"]["redis"]["error"]


async def test_postgres_timeout_returns_503(app: FastAPI, client: AsyncClient) -> None:
    _override(app, POSTGRES_DOWN, REDIS_UP)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["dependencies"]["postgres"]["error"] == "probe exceeded 2s timeout"


async def test_request_id_is_generated_and_echoed(app: FastAPI, client: AsyncClient) -> None:
    _override(app, POSTGRES_UP, REDIS_UP)

    response = await client.get("/health")

    request_id = response.headers["x-request-id"]
    assert request_id
    assert response.json()["request_id"] == request_id


async def test_inbound_request_id_is_preserved(app: FastAPI, client: AsyncClient) -> None:
    _override(app, POSTGRES_UP, REDIS_UP)

    response = await client.get("/health", headers={"X-Request-ID": "caller-supplied-1"})

    assert response.headers["x-request-id"] == "caller-supplied-1"
    assert response.json()["request_id"] == "caller-supplied-1"


async def test_request_completion_is_logged_with_correlation(
    app: FastAPI,
    client: AsyncClient,
    test_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The access log must be correlatable, which uvicorn's own cannot be."""
    _override(app, POSTGRES_UP, REDIS_UP)
    # Re-bind the log handler to this phase's captured stdout: the app fixture
    # configured logging during setup, against a stream capsys has since
    # replaced. Reconfiguration works because loggers are not cached.
    configure_logging(test_settings)

    await client.get("/health", headers={"X-Request-ID": "logged-request-1"})

    records = [
        json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if line.strip()
    ]
    completions = [r for r in records if r["event"] == "http_request_completed"]

    assert len(completions) == 1
    assert completions[0]["request_id"] == "logged-request-1"
    assert completions[0]["method"] == "GET"
    assert completions[0]["path"] == "/health"
    assert completions[0]["status_code"] == 200
    assert completions[0]["duration_ms"] >= 0
