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


# Ceiling on how long the setup DDL may wait. See `ensure_checkpointer_ready`.
_LOCK_TIMEOUT_MS = 10_000

# Serialises setup across processes. `CREATE INDEX CONCURRENTLY` cannot run
# while another is in progress on the same table, so N workers all starting at
# once would race; this makes them queue, and the losers find the indexes
# already there. Session-level rather than transactional, because
# `CREATE INDEX CONCURRENTLY` refuses to run inside a transaction block.
_SETUP_ADVISORY_KEY = 0x0C0570C5  # arbitrary, stable, "custops"-ish

# Who else is connected, what they are running, and — via `pg_blocking_pids` —
# which of them this session is actually waiting behind. `left(query, 200)`
# because the point is to identify the statement, not to reproduce it.
_BLOCKING_SESSIONS_SQL = """
SELECT pid,
       state,
       wait_event_type,
       wait_event,
       pg_blocking_pids(pid) AS blocked_by,
       left(query, 200)      AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY state_change
"""


async def _blocking_sessions(conn_string: str) -> list[dict[str, Any]]:
    """Snapshot the other sessions on this database, for a blocked setup.

    Opens its own short-lived connection deliberately: the checkpointer's own
    one is stuck on the lock and cannot answer. Diagnostics must never be the
    reason a request fails, so any error here is swallowed and reported in
    place of the snapshot.
    """
    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(conn_string) as diagnostic:
            cursor = await diagnostic.execute(_BLOCKING_SESSIONS_SQL)
            rows = await cursor.fetchall()
            columns = [column.name for column in cursor.description or []]
            return [dict(zip(columns, row, strict=True)) for row in rows]
    except Exception as error:  # pragma: no cover - diagnostics only
        return [{"unavailable": str(error)}]


async def ensure_checkpointer_ready(settings: Settings) -> None:
    """Create the checkpointer's tables. Call once, at startup.

    This used to run on every workflow execution, and that was a deadlock. The
    library's ``setup()`` issues ``CREATE INDEX CONCURRENTLY``, which by design
    waits for every open transaction that can see the table to finish. Inside a
    request there is always one: the authentication dependency's own session,
    holding a ``users``/``roles`` lookup open for the life of the request. So
    the request waited for a transaction that could not end until the request
    did. It only bit the first few runs, because ``IF NOT EXISTS`` makes the
    statement a no-op once the indexes exist — which is precisely why it looked
    like a mysterious first-use stall rather than a deadlock.

    At startup no request-scoped transaction exists, so there is nothing to wait
    behind. Errors propagate: tables that do not exist cannot persist a workflow
    paused for approval, and §7 requires that persistence, so booting into that
    state would be a silent liability rather than resilience.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.conninfo import make_conninfo

    conn_string = settings.postgres.libpq_dsn()

    # Bound the wait. Unbounded, a conflicting session does not make setup slow,
    # it makes it hang — which is the failure this function exists to end. Ten
    # seconds is ~5000x the measured setup time (~2ms), so it cannot fire on a
    # healthy database. It is scoped to this connection alone; the per-run
    # checkpointer keeps the plain DSN and is not bounded by it.
    bounded_dsn = make_conninfo(conn_string, options=f"-c lock_timeout={_LOCK_TIMEOUT_MS}")

    started = perf_counter()
    async with await AsyncConnection.connect(conn_string, autocommit=True) as guard:
        await guard.execute("SELECT pg_advisory_lock(%s)", (_SETUP_ADVISORY_KEY,))
        try:
            async with AsyncPostgresSaver.from_conn_string(bounded_dsn) as saver:
                await saver.setup()
        except Exception as error:
            logger.error(
                "checkpointer_setup_blocked",
                elapsed_ms=round((perf_counter() - started) * 1000, 1),
                error=str(error),
                blockers=await _blocking_sessions(conn_string),
            )
            raise
        finally:
            await guard.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_ADVISORY_KEY,))

    logger.info(
        "checkpointer_ready",
        elapsed_ms=round((perf_counter() - started) * 1000, 1),
        database=settings.postgres.db,
    )


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

    # No `setup()` here. It belongs to startup — see `ensure_checkpointer_ready`
    # for why running it inside a request deadlocks against that request's own
    # authentication transaction. The plain DSN is deliberate too: the lock
    # ceiling is a property of the setup DDL, and imposing it on ordinary
    # checkpoint writes would be an unrelated constraint on the request path.
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        yield saver
