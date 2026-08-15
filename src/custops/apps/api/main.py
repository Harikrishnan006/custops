"""FastAPI application factory and lifespan.

Resource ownership is explicit: the lifespan creates the engine and the Redis
client once, stores them on ``app.state``, and disposes of them on shutdown.
Neither is connected eagerly, so the API starts even when a dependency is down
and ``/health`` can report *why* — a process that refuses to boot cannot tell you
what is wrong with it.

``create_app`` takes optional settings so tests can build an app against explicit
configuration instead of mutating the environment and hoping the cache is cold.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from custops.apps.api.middleware.request_context import RequestContextMiddleware
from custops.apps.api.routers import approvals, health, workflows
from custops.apps.enterprise.router import router as enterprise_router
from custops.cache.redis_client import create_redis_client
from custops.config import Settings, get_settings
from custops.db.engine import create_database
from custops.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and dispose of long-lived resources."""
    settings: Settings = app.state.settings

    database = create_database(settings)
    redis_client = create_redis_client(settings)
    app.state.database = database
    app.state.redis = redis_client

    logger.info(
        "api_startup",
        environment=settings.environment,
        # Masked DSNs: enough to diagnose "pointed at the wrong host", never a
        # credential in the log stream (Rule 16).
        postgres=settings.postgres.safe_dsn,
        redis=settings.redis.safe_dsn,
    )
    try:
        yield
    finally:
        await database.dispose()
        await redis_client.aclose()
        logger.info("api_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Logging is configured here rather than in the lifespan so that anything
    logged during import and startup is already structured.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)

    app = FastAPI(
        title="AI Customer Operations Orchestrator",
        description=(
            "Converts natural-language customer-operations requests into "
            "executable, stateful, auditable workflows."
        ),
        version=resolved.version,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    # Read-only views onto the systems of record. Mutations are not routed here
    # — they travel through the MCP tool layer, which enforces approval (D9).
    app.include_router(enterprise_router)
    # Workflow start and trace reconstruction. The approval-decision route is
    # Phase 7; a paused run reports itself and waits.
    app.include_router(workflows.router)
    # Layer 2 of §13: records the human decision. The MCP tool layer still
    # verifies independently before acting (D9).
    app.include_router(approvals.router)
    return app


app = create_app()
