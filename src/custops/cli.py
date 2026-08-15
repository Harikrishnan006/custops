"""Operator CLI.

Administrative commands that are not part of the HTTP surface. Kept deliberately
small: anything an agent needs goes through MCP tools with permissions and audit,
not through a shell command (BUILD_SPEC §17 — an agent must never execute shell
commands).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from custops.config import get_settings
from custops.db.engine import create_database
from custops.domain.seed import clear_seed_data, seed_all
from custops.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def _seed(*, reset: bool) -> int:
    """Load the synthetic catalogue into the configured database."""
    settings = get_settings()
    database = create_database(settings)

    try:
        async with database.session_factory() as session:
            if reset:
                await clear_seed_data(session)
                logger.info("seed_cleared")

            counts = await seed_all(session, now=datetime.now(UTC))
            await session.commit()

        logger.info("seed_completed", **counts)
    finally:
        await database.dispose()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="custops", description="custops operator commands")
    subcommands = parser.add_subparsers(dest="command", required=True)

    seed_parser = subcommands.add_parser("seed", help="load synthetic seed data")
    seed_parser.add_argument(
        "--reset",
        action="store_true",
        help="delete seeded customers first (only rows this seed created)",
    )

    arguments = parser.parse_args(argv)

    configure_logging(get_settings())

    if arguments.command == "seed":
        return asyncio.run(_seed(reset=bool(arguments.reset)))

    # argparse's `required=True` makes this unreachable in practice; error()
    # is NoReturn, so there is nothing to return afterwards.
    parser.error(f"unknown command: {arguments.command}")


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
