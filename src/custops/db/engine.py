"""Async engine, session factory, and the PostgreSQL liveness probe."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from custops.config import Settings
from custops.observability.probes import ProbeResult, run_probe

PGVECTOR_EXTENSION = "vector"

_PGVECTOR_VERSION_SQL = text("SELECT extversion FROM pg_extension WHERE extname = :extension")


@dataclass(frozen=True, slots=True)
class Database:
    """An engine plus its session factory, held together.

    Passed around as one value so no caller has to build a sessionmaker from a
    loose engine and get the settings (``expire_on_commit``) wrong.
    """

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, closing it afterwards. Callers own the transaction."""
        async with self.session_factory() as session:
            yield session


def create_database(settings: Settings) -> Database:
    """Build the engine and session factory.

    Creating an engine does not open a connection — SQLAlchemy pools lazily. That
    is deliberate: the API starts even when PostgreSQL is unreachable, and
    ``/health`` reports the real state instead of the process failing to boot. A
    service that cannot start cannot tell you why it is unhealthy.
    """
    engine = create_async_engine(
        settings.postgres.dsn(),
        echo=settings.postgres.echo_sql,
        pool_size=settings.postgres.pool_size,
        max_overflow=settings.postgres.max_overflow,
        # Recycle dead connections rather than handing a stale one to a workflow.
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    return Database(engine=engine, session_factory=session_factory)


async def probe_postgres(
    engine: AsyncEngine,
    *,
    timeout: float,  # noqa: ASYNC109 - bound applied in run_probe; see probes.run_probe
) -> ProbeResult:
    """Confirm PostgreSQL connectivity with a real round trip.

    Also reports the installed pgvector version, because "can I connect" and
    "is the vector extension actually present" are different questions and the
    knowledge layer depends on the second one. A missing extension is reported in
    ``detail`` rather than failing the probe: connectivity is up, capability is
    absent, and conflating the two would make the signal useless for triage.
    """

    async def operation() -> dict[str, Any]:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            pgvector_version = (
                await connection.execute(_PGVECTOR_VERSION_SQL, {"extension": PGVECTOR_EXTENSION})
            ).scalar_one_or_none()
            server_version = (await connection.execute(text("SHOW server_version"))).scalar_one()
        return {
            "server_version": str(server_version),
            "pgvector_extension": pgvector_version,
        }

    return await run_probe("postgres", operation, timeout=timeout)
