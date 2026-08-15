"""Health endpoint.

Confirms this process can reach PostgreSQL and Redis *at the moment of the call*
and reports what it found. Two design choices are worth defending:

* **Probes run concurrently.** Two sequential probes make the worst-case latency
  the sum of both timeouts; the endpoint's own latency budget should be one
  timeout, not N.
* **503 on any dependency failure, with the same body.** Returning 200 while a
  dependency is unreachable makes the endpoint useless for orchestration, and
  swapping the body for an error shape on failure means the caller has to parse
  two schemas to find out what broke.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from custops.apps.api.dependencies import HealthProbes, get_health_probes, get_settings_from_state
from custops.apps.api.schemas.health import DependencyStatus, HealthResponse
from custops.config import Settings
from custops.observability.context import get_execution_id, get_request_id

router = APIRouter(tags=["operations"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and dependency health",
    responses={
        status.HTTP_200_OK: {"description": "Service and all dependencies healthy."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthResponse,
            "description": "At least one dependency did not answer.",
        },
    },
)
async def health(
    response: Response,
    probes: Annotated[HealthProbes, Depends(get_health_probes)],
    settings: Annotated[Settings, Depends(get_settings_from_state)],
) -> HealthResponse:
    postgres_result, redis_result = await asyncio.gather(probes.postgres(), probes.redis())
    results = (postgres_result, redis_result)

    healthy = all(result.is_up for result in results)
    response.status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if healthy else "degraded",
        service=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        request_id=get_request_id(),
        execution_id=get_execution_id(),
        dependencies={result.name: DependencyStatus.from_probe(result) for result in results},
    )
