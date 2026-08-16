"""Integration fixtures.

These tests target the PostgreSQL and Redis instances named by the *real*
configuration (``.env`` / environment), not the synthetic unit-test settings —
the point is to exercise the deployment a developer actually has running.

When a dependency is unusable the tests skip rather than fail, and the skip
reason says exactly what is missing.

**Reachability is not usability.** An earlier version of this guard decided
availability with a plain TCP connect. That is wrong in a way that matters: a
PostgreSQL that is listening but has no ``custops`` role un-skips every
integration test, and all of them then fail on authentication — 64 errors that
look like code defects and are not. The probe below therefore opens a real
connection with the configured credentials and checks that pgvector is
available, because migration 0001 cannot run without it.

The probe runs once, at collection time, with a short timeout so it cannot hang
the suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from custops.apps.api.main import create_app
from custops.apps.api.routers.workflows import get_retrieval_policy
from custops.config import Settings, get_settings
from custops.db.engine import Database, create_database
from custops.domain.policies.retrieval import RetrievalPolicy
from tests.support import service_reachable

_settings = get_settings()

PROBE_TIMEOUT_SECONDS = 5.0


def _probe_postgres() -> str | None:
    """Return None when PostgreSQL is usable, else why it is not.

    Checks three things in order, so the reason names the first real blocker
    rather than a downstream symptom: the port answers, the configured
    credentials authenticate against the configured database, and the pgvector
    extension is available to be created.
    """
    host = _settings.postgres.host
    port = _settings.postgres.port

    if not service_reachable(host, port):
        return f"nothing listening at {host}:{port}"

    async def check() -> str | None:
        import asyncpg

        try:
            connection = await asyncpg.connect(
                host=host,
                port=port,
                user=_settings.postgres.user,
                password=_settings.postgres.password.get_secret_value(),
                database=_settings.postgres.db,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # any connect failure means "not usable"
            return f"{host}:{port} is listening but unusable: {type(error).__name__}: {error}"

        try:
            available = await connection.fetchval(
                "select count(*) from pg_available_extensions where name = 'vector'"
            )
            if not available:
                return (
                    "pgvector is not available on this server; migration 0001 "
                    "cannot run (see docs/INTEGRATION-VERIFICATION.md §3.2)"
                )
        finally:
            await connection.close()
        return None

    try:
        return asyncio.run(asyncio.wait_for(check(), timeout=PROBE_TIMEOUT_SECONDS * 2))
    except Exception as error:  # a probe that fails is a probe that says so
        return f"probe failed: {type(error).__name__}: {error}"


_postgres_unavailable_reason = _probe_postgres()
postgres_available = _postgres_unavailable_reason is None
redis_available = service_reachable(_settings.redis.host, _settings.redis.port)

requires_postgres = pytest.mark.skipif(
    not postgres_available,
    reason=(
        f"PostgreSQL unusable — {_postgres_unavailable_reason}. "
        "Setup: docs/INTEGRATION-VERIFICATION.md"
    ),
)
requires_redis = pytest.mark.skipif(
    not redis_available,
    reason=(
        f"Redis not reachable at {_settings.redis.host}:{_settings.redis.port} — "
        "see README 'Running the dependencies'"
    ),
)


@pytest.fixture
def runtime_settings() -> Settings:
    return _settings


@pytest.fixture
async def database(runtime_settings: Settings) -> AsyncIterator[Database]:
    db = create_database(runtime_settings)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def live_app(runtime_settings: Settings) -> AsyncIterator[FastAPI]:
    """An application with its lifespan actually run, holding real connections."""
    application = create_app(settings=runtime_settings)
    # The deterministic embedder's scores sit on a different scale from a real
    # model's; without this every workflow escalates at research.
    application.dependency_overrides[get_retrieval_policy] = lambda: TEST_RETRIEVAL_POLICY
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=live_app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Authentication (§17, Phase 13)
# ---------------------------------------------------------------------------
# Every protected endpoint requires a bearer token. Tests authenticate through
# the *same* dependency production uses — there is deliberately no
# "authentication disabled in test" switch, because a bypass flag is precisely
# the thing that eventually ships enabled.
#
# Tokens are minted here and inserted as hashes, exactly as `custops issue-token`
# does. No credential is committed to the repository; the plaintext exists only
# for the lifetime of the test that uses it.

OPERATOR_EMAIL = "ops.approver@custops.example.com"
FINANCE_EMAIL = "finance.approver@custops.example.com"
VIEWER_EMAIL = "viewer@custops.example.com"


async def issue_test_token(database: Database, *, email: str, label: str = "test") -> str:
    """Mint a real credential for a seeded user and return the plaintext."""
    from custops.apps.api.security.issuance import issue

    async with database.session_factory() as session:
        issued = await issue(session, email=email, label=label)
        await session.commit()
    return issued.plaintext


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def authenticated_client(
    app: FastAPI, database: Database, *, email: str
) -> AsyncClient:
    """An HTTP client carrying a real token for one seeded user."""
    token = await issue_test_token(database, email=email)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=bearer(token),
    )


@pytest.fixture
async def operator_client(live_app: FastAPI, database: Database) -> AsyncIterator[AsyncClient]:
    """Holds `operator` and `approver` — may start workflows and approve routine ones."""
    client = await authenticated_client(live_app, database, email=OPERATOR_EMAIL)
    async with client:
        yield client


@pytest.fixture
async def finance_client(live_app: FastAPI, database: Database) -> AsyncIterator[AsyncClient]:
    """Holds `finance_approver` — elevated approval authority, no operator role."""
    client = await authenticated_client(live_app, database, email=FINANCE_EMAIL)
    async with client:
        yield client


@pytest.fixture
async def viewer_client(live_app: FastAPI, database: Database) -> AsyncIterator[AsyncClient]:
    """Read-only. Used to prove that reading and acting are genuinely separate."""
    client = await authenticated_client(live_app, database, email=VIEWER_EMAIL)
    async with client:
        yield client


# ---------------------------------------------------------------------------
# Retrieval calibration for the deterministic embedder
# ---------------------------------------------------------------------------
# A similarity threshold is a property of the embedding model, not of the
# business rule. `RetrievalPolicy()`'s production default of 0.35 is calibrated
# for a real embedding model; the deterministic lexical double produces scores
# on a different scale, so the same number would read every result as
# insufficient and escalate every workflow before it reached a decision.
#
# The value below is measured, not chosen — and it must be measured against the
# population the pipeline actually embeds. That is the subtlety that cost a CI
# round: ingestion chunks the policy *body* and embeds each chunk on its own, so
# a chunk carries neither the policy title nor the rest of the document. Scores
# taken against whole `title + body` documents run roughly twice as high as the
# scores the retrieval gate will really see, and a threshold calibrated on them
# escalates every workflow anyway.
#
# Measured over `chunk_text(policy["body"])` for the seeded corpus, against the
# query the research node actually issues:
#
#     relevant chunks     +0.0933, +0.0894   (the two policies that bear on it)
#     unrelated chunks    +0.0000            (exactly zero — no shared stems)
#
# 0.05 is the midpoint of those populations, rounded to a round number. It sits
# below the weaker genuine match with ~44% headroom and above every unrelated
# one. It is emphatically *not* permissive — unrelated content scores zero and
# still escalates; `tests/unit/test_retrieval_calibration.py` measures the same
# chunk population and proves both directions.
TEST_RETRIEVAL_MINIMUM_SIMILARITY = 0.05

TEST_RETRIEVAL_POLICY = RetrievalPolicy(
    minimum_similarity=TEST_RETRIEVAL_MINIMUM_SIMILARITY
)


def use_test_retrieval_policy(app: FastAPI) -> None:
    """Point an app's retrieval gate at the calibrated threshold.

    Overrides the same dependency production resolves, so the injection path is
    exercised rather than bypassed.
    """
    app.dependency_overrides[get_retrieval_policy] = lambda: TEST_RETRIEVAL_POLICY
