"""Alembic environment.

Two deviations from the generated async template, both deliberate:

1. **The URL comes from ``custops.config.Settings``, not ``alembic.ini``.** One
   configuration source for the whole system (BUILD_SPEC §17); a URL duplicated
   in an ini file is a URL that will disagree with the application's.

2. **The engine is built directly rather than via ``async_engine_from_config``.**
   ``alembic.ini`` is read by configparser, which performs ``%`` interpolation.
   A percent-encoded password (``%40`` for ``@``) injected into that file raises
   an interpolation error, so the DSN never goes through the ini at all.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Importing the models package populates Base.metadata. Without this, every
# autogenerate run would produce an empty diff — a silent, dangerous no-op.
import custops.domain.models  # noqa: F401  (imported for metadata registration)
from custops.config import get_settings
from custops.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_settings = get_settings()
_database_url = _settings.postgres.dsn()


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without these, a changed column type or server default is silently
        # ignored by autogenerate.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(_database_url, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
