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
from custops.knowledge.ingestion.pipeline import ingest_contracts, ingest_policies
from custops.observability.logging import configure_logging, get_logger
from custops.providers.registry import get_embedding_provider

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


async def _ingest() -> int:
    """Embed policies and contracts into the knowledge corpus.

    Idempotent: unchanged documents are skipped without re-embedding, so this
    is safe to run on every deploy.
    """
    settings = get_settings()
    provider = get_embedding_provider(settings)
    database = create_database(settings)

    logger.info("ingestion_started", provider=provider.model, dimensions=provider.dimensions)
    try:
        async with database.session_factory() as session:
            policies = await ingest_policies(session, provider)
            contracts = await ingest_contracts(session, provider)
            await session.commit()

        logger.info(
            "ingestion_finished",
            policy_documents=policies.documents_processed,
            policy_chunks=policies.chunks_written,
            contract_documents=contracts.documents_processed,
            contract_chunks=contracts.chunks_written,
        )
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

    subcommands.add_parser("ingest", help="embed policies and contracts for retrieval")

    # `evaluate` parses its own flags: the evaluation layer is a dev-time tool
    # whose dependency (agent-forge) is not installed in production, so it must
    # not be imported when the operator runs `seed` or `ingest`.
    subcommands.add_parser(
        "evaluate",
        help="score the orchestrator against the §15 datasets and gate on regressions",
        add_help=False,
    )

    arguments, extra = parser.parse_known_args(argv)

    configure_logging(get_settings())

    if arguments.command == "seed":
        return asyncio.run(_seed(reset=bool(arguments.reset)))

    if arguments.command == "ingest":
        return asyncio.run(_ingest())

    if arguments.command == "evaluate":
        # Imported here, never at module scope: agent-forge is a dev dependency
        # and a production install has no reason to fail importing the CLI.
        from custops.evaluation.cli import run as run_evaluation

        return run_evaluation(extra)

    # argparse's `required=True` makes this unreachable in practice; error()
    # is NoReturn, so there is nothing to return afterwards.
    parser.error(f"unknown command: {arguments.command}")


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
