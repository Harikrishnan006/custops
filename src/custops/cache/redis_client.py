"""Async Redis client and its liveness probe.

Module named ``redis_client`` rather than ``redis`` so it can never shadow the
installed ``redis`` distribution — the same class of import collision that
ADR-001 avoids for the ``mcp`` and ``a2a`` SDKs.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from custops.config import Settings
from custops.observability.probes import ProbeResult, run_probe


def create_redis_client(settings: Settings) -> Redis:
    """Build the async Redis client.

    Like the SQLAlchemy engine, this does not connect eagerly; timeouts are set
    so a wedged Redis surfaces as an error rather than a hang.
    """
    return Redis.from_url(
        settings.redis.dsn(),
        decode_responses=True,
        socket_timeout=settings.redis.socket_timeout_seconds,
        socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
    )


async def probe_redis(
    client: Redis,
    *,
    timeout: float,  # noqa: ASYNC109 - bound applied in run_probe; see probes.run_probe
) -> ProbeResult:
    """Confirm Redis connectivity with a real ``PING``."""

    async def operation() -> dict[str, Any]:
        await client.ping()
        raw_info: Any = await client.info("server")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        return {"redis_version": info.get("redis_version")}

    return await run_probe("redis", operation, timeout=timeout)
