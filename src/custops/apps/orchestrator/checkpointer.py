"""Checkpointer selection — where a paused workflow lives.

**PostgreSQL, resolving ADR-003.** A workflow interrupted at an approval gate
may wait hours or days for a human. That is business state, not cache: losing it
loses an in-flight customer operation, and §7 explicitly requires such a pause to
survive a process restart. Decision D2 assigned checkpoints to Redis; §5 makes
PostgreSQL the source of truth and §16 requires a reconstructable trace. Both
point the same way, and it is not Redis.

Two implementation facts worth knowing before reading further, both verified
against the installed packages rather than recalled:

* The LangGraph Postgres checkpointer speaks **psycopg 3**, while the rest of
  this application reaches PostgreSQL through SQLAlchemy + **asyncpg**. Two
  drivers against one database is a real cost; it is accepted because the
  alternative is reimplementing a checkpointer, and the checkpointer is a
  library concern rather than a domain one. Both connect to the same database.
* ``AsyncPostgresSaver`` **manages its own tables** via its internal migrations
  and ``setup()``. Those tables are deliberately *not* in our Alembic history:
  they are the library's schema, versioned with the library, and hand-writing
  them into our migrations would guarantee drift the first time it upgrades.

``InMemorySaver`` is used only where persistence is not the thing under test.
It is not a fallback for a missing database: a run that must survive restart
cannot be checkpointed in memory, so the factory refuses it outside
``local``/``test`` — the same rule the deterministic embedder follows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from custops.config import Settings
from custops.observability.logging import get_logger

logger = get_logger(__name__)

PERSISTENCE_OPTIONAL_ENVIRONMENTS = frozenset({"local", "test"})


class CheckpointerError(RuntimeError):
    """Raised when the configured checkpointer cannot be provided safely."""


@asynccontextmanager
async def open_checkpointer(
    settings: Settings, *, in_memory: bool = False
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Yield a checkpointer for the configured environment.

    ``in_memory`` is for tests that exercise graph mechanics rather than
    durability. Requesting it in a deployed environment is an error rather than
    a silent downgrade — a workflow that cannot survive a restart is not a
    human-in-the-loop workflow.
    """
    if in_memory:
        if settings.environment not in PERSISTENCE_OPTIONAL_ENVIRONMENTS:
            raise CheckpointerError(
                "An in-memory checkpointer cannot persist a workflow paused for "
                f"approval and is not permitted in environment "
                f"'{settings.environment}'."
            )
        logger.info("checkpointer_selected", kind="in_memory")
        yield InMemorySaver()
        return

    # Imported here rather than at module scope: the psycopg driver is only
    # needed for the Postgres path, and an import error should surface as a
    # checkpointer problem rather than as an unimportable module.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    # psycopg speaks the plain postgresql:// scheme; our SQLAlchemy URL carries
    # the +asyncpg driver suffix, which psycopg does not understand.
    conn_string = settings.postgres.libpq_dsn()

    logger.info("checkpointer_selected", kind="postgres", database=settings.postgres.db)

    # Timing, not ceremony. Three integration tests hang for their full 120s
    # between `checkpointer_selected` and the run finishing, and the two
    # candidates — acquiring the psycopg connection, and the setup DDL — are
    # indistinguishable from the outside. These are structured logs rather than
    # audit events; the §16 taxonomy stays at 19.
    #
    # Read them as a discriminator: neither line means the connection never came
    # back, the first alone means `setup()` is blocking, and both mean the stall
    # is further in, after the checkpointer was ready.
    connect_started = perf_counter()
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        logger.info(
            "checkpointer_connected",
            elapsed_ms=round((perf_counter() - connect_started) * 1000, 1),
        )

        # Creates the checkpointer's own tables if absent. Idempotent, and owned
        # by the library rather than by our Alembic history.
        setup_started = perf_counter()
        await saver.setup()
        logger.info(
            "checkpointer_setup_completed",
            elapsed_ms=round((perf_counter() - setup_started) * 1000, 1),
        )

        ready_at = perf_counter()
        try:
            yield saver
        finally:
            # How long the caller held it — separates "the checkpointer was slow"
            # from "the graph run itself was".
            logger.info(
                "checkpointer_released",
                elapsed_ms=round((perf_counter() - ready_at) * 1000, 1),
            )
