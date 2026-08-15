"""Database connectivity and schema state (Phase 1 definition of done, items 2, 3, 7).

Requires a running PostgreSQL with migrations applied:

    uv run alembic upgrade head

These assertions are the ones that cannot be faked by a stub: the extension is
either installed in the server or it is not, and the tables either exist or they
do not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from custops.db.engine import Database, probe_postgres
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

EXPECTED_TABLES = {"users", "roles", "user_roles", "audit_events", "alembic_version"}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def alembic_heads() -> list[str]:
    """Ask Alembic what the head revision is, rather than restating it here.

    This assertion used to hardcode ``0002_foundation_tables``, which went stale
    the moment migration 0003 landed and stayed wrong through 0006 — a test that
    could only fail for the wrong reason, and only on a machine that could reach
    PostgreSQL. Deriving it means adding a migration can never leave a false
    expectation behind.

    Returns every head. That the history has exactly one is asserted without a
    database in ``tests/unit/test_migration_schema_consistency.py``, so a branch
    is caught on any machine rather than only where PostgreSQL is reachable.
    """
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return list(ScriptDirectory.from_config(config).get_heads())


async def test_database_accepts_a_connection(database: Database) -> None:
    async with database.engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1


async def test_probe_reports_postgres_up(database: Database) -> None:
    result = await probe_postgres(database.engine, timeout=5.0)

    assert result.is_up, result.error
    assert result.detail is not None
    assert result.detail["server_version"]
    assert result.latency_ms > 0


async def test_pgvector_extension_is_installed(database: Database) -> None:
    """D4: pgvector is an extension of this database, not a separate service."""
    async with database.engine.connect() as connection:
        version = (
            await connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one_or_none()

    assert version is not None, "run `uv run alembic upgrade head` to apply migration 0001"


async def test_foundation_tables_exist(database: Database) -> None:
    async with database.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        ).scalars()
        tables = set(rows)

    assert tables >= EXPECTED_TABLES, f"missing: {EXPECTED_TABLES - tables}"


async def test_schema_is_at_head_revision(database: Database) -> None:
    async with database.engine.connect() as connection:
        revisions = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars()

    assert list(revisions) == alembic_heads()


async def test_audit_events_defaults_are_server_side(database: Database) -> None:
    """A row inserted by raw SQL still gets its timestamp and payload default."""
    async with database.engine.begin() as connection:
        row = (
            await connection.execute(
                text(
                    "INSERT INTO audit_events (event_type, actor_type) "
                    "VALUES ('workflow_completed', 'system') "
                    "RETURNING id, occurred_at, payload"
                )
            )
        ).one()
        assert row.occurred_at is not None
        assert row.payload == {}

        # Integration tests must not leave rows behind for the next run.
        await connection.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": row.id})
