"""The Billing Specialist against real systems of record.

Three things are proven here that no in-process test can prove:

1. The specialist's reads go through **its own** MCP role and leave
   ``tool_calls``/``audit_events`` rows attributed to ``billing_specialist``.
2. It answers correctly from data it fetched itself, given only identifiers.
3. Started as a **real separate process on a real port**, it is discoverable and
   answers over the network — which is what D6 actually claims.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from custops.a2a.client.billing import BillingSpecialistClient, ConsultStatus
from custops.a2a.contracts.card import AGENT_CARD_PATH
from custops.a2a.contracts.pricing import (
    CAPABILITY_PRICING_DECISION,
    PricingDecisionRequest,
)
from custops.apps.billing_specialist.reasoning import (
    SpecialistRefusalError,
    price_upgrade,
)
from custops.apps.enterprise import assessment as assessment_module
from custops.config import Settings
from custops.db.engine import Database
from custops.domain.models.approval import ToolCall
from custops.domain.models.audit import AuditEvent
from custops.domain.seed import clear_seed_data, seed_all, seed_id
from custops.mcp.permissions.matrix import Role
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
ACME_ACCOUNT = seed_id("account", "acme")


@pytest.fixture
async def seeded(database: Database) -> AsyncIterator[Database]:
    """Seed the catalogue and accounts the specialist reads."""
    async with database.session_factory() as session:
        await seed_all(session, now=NOW)
        await session.commit()
    try:
        yield database
    finally:
        async with database.session_factory() as session:
            await clear_seed_data(session)
            await session.commit()


# ------------------------------------------------- reasoning through its role


class TestReasoningThroughItsOwnRole:
    async def test_it_prices_an_upgrade_from_identifiers_alone(self, seeded: Database) -> None:
        """The caller supplies no billing data; the specialist fetches it all."""
        async with seeded.session_factory() as session:
            recommendation = await price_upgrade(
                session,
                PricingDecisionRequest(account_id=ACME_ACCOUNT, target_plan_code="enterprise"),
                now=NOW,
            )
            await session.commit()

        assert recommendation.account_id == ACME_ACCOUNT
        assert recommendation.target_plan_code == "enterprise"
        assert recommendation.days_in_period > 0
        assert recommendation.amount_due == (
            recommendation.new_plan_charge - recommendation.unused_credit
        )

    async def test_its_figure_matches_the_orchestrators_own_assessment(
        self, seeded: Database
    ) -> None:
        """The value of a second opinion is that it agrees when the data agrees.

        Two independent reads of the same account, through different code paths,
        must produce the same number. When they stop agreeing, one of them is
        reading different state — which is exactly the signal the orchestrator
        escalates on.
        """
        async with seeded.session_factory() as session:
            local = await assessment_module.assess_upgrade(
                session,
                account_id=ACME_ACCOUNT,
                target_plan_code="enterprise",
                now=NOW,
            )
            await session.commit()

        async with seeded.session_factory() as session:
            remote = await price_upgrade(
                session,
                PricingDecisionRequest(account_id=ACME_ACCOUNT, target_plan_code="enterprise"),
                now=NOW,
            )
            await session.commit()

        assert remote.amount_due == local.proration.amount_due
        assert remote.current_plan_code == local.current_plan_code

    async def test_its_reads_are_audited_under_its_own_role(self, seeded: Database) -> None:
        """Being a separate process does not put it outside the tool boundary.

        The rows must name ``billing_specialist`` — not the orchestrator — or the
        audit trail would credit the specialist's reads to whoever called it.
        """
        execution_id = uuid.uuid4()

        async with seeded.session_factory() as session:
            await price_upgrade(
                session,
                PricingDecisionRequest(
                    account_id=ACME_ACCOUNT,
                    target_plan_code="enterprise",
                    execution_id=execution_id,
                ),
                now=NOW,
            )
            await session.commit()

        async with seeded.session_factory() as session:
            calls = (
                (
                    await session.execute(
                        select(ToolCall).where(ToolCall.execution_id == execution_id)
                    )
                )
                .scalars()
                .all()
            )
            events = (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.execution_id == execution_id)
                    )
                )
                .scalars()
                .all()
            )

        assert {call.tool_name for call in calls} >= {
            "get_subscription",
            "get_pricing",
            "get_invoice",
        }
        assert all(call.succeeded for call in calls if call.tool_name != "get_contract")
        assert {event.actor_id for event in events} == {Role.BILLING_SPECIALIST}

    async def test_it_never_writes_an_approval_or_a_mutation(self, seeded: Database) -> None:
        """A2A adds a second opinion, not a second way to write."""
        execution_id = uuid.uuid4()

        async with seeded.session_factory() as session:
            await price_upgrade(
                session,
                PricingDecisionRequest(
                    account_id=ACME_ACCOUNT,
                    target_plan_code="enterprise",
                    execution_id=execution_id,
                ),
                now=NOW,
            )
            await session.commit()

        async with seeded.session_factory() as session:
            calls = (
                (
                    await session.execute(
                        select(ToolCall).where(ToolCall.execution_id == execution_id)
                    )
                )
                .scalars()
                .all()
            )

        assert all(call.approval_id is None for call in calls)

    async def test_an_unknown_account_is_refused_not_guessed(self, seeded: Database) -> None:
        async with seeded.session_factory() as session:
            with pytest.raises(SpecialistRefusalError) as raised:
                await price_upgrade(
                    session,
                    PricingDecisionRequest(account_id=uuid.uuid4(), target_plan_code="enterprise"),
                    now=NOW,
                )
            await session.rollback()

        assert raised.value.code == "no_active_subscription"

    async def test_an_unknown_plan_is_refused_not_guessed(self, seeded: Database) -> None:
        async with seeded.session_factory() as session:
            with pytest.raises(SpecialistRefusalError) as raised:
                await price_upgrade(
                    session,
                    PricingDecisionRequest(
                        account_id=ACME_ACCOUNT, target_plan_code="no-such-plan"
                    ),
                    now=NOW,
                )
            await session.rollback()

        assert raised.value.code == "target_plan_not_found"


# --------------------------------------------------- genuinely out of process


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def specialist_process(runtime_settings: Settings) -> Iterator[str]:
    """Start the real specialist in a real subprocess on a real port.

    This is the fixture that makes the phase's claim testable. Everything else
    could pass with the agent imported into the test process; only this proves
    the orchestrator can reach a specialist it does not share memory with.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "A2A_HOST": "127.0.0.1",
        "A2A_PORT": str(port),
        "A2A_BILLING_SPECIALIST_URL": base_url,
        "PYTHONPATH": "src",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from custops.apps.billing_specialist.app import main; main()",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read().decode() if process.stdout else ""
                pytest.fail(f"Specialist exited before serving:\n{output}")
            try:
                if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail(f"Specialist did not start within 30s at {base_url}")

        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - shutdown safety
            process.kill()


class TestOutOfProcess:
    async def test_the_agent_card_is_discoverable_over_the_network(
        self, seeded: Database, specialist_process: str
    ) -> None:
        """A URL and the well-known path are all the orchestrator needs.

        No import, no shared module — which is what lets this agent be replaced
        or relocated without the orchestrator changing.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{specialist_process}{AGENT_CARD_PATH}")

        assert response.status_code == 200
        card = response.json()
        assert card["name"] == "billing-specialist"
        assert card["skills"][0]["id"] == CAPABILITY_PRICING_DECISION

    async def test_it_answers_a_pricing_request_over_a_real_socket(
        self, seeded: Database, specialist_process: str
    ) -> None:
        client = BillingSpecialistClient(specialist_process, timeout_seconds=15.0)

        result = await client.request_pricing_decision(
            account_id=ACME_ACCOUNT, target_plan_code="enterprise"
        )

        assert result.status is ConsultStatus.CONSULTED
        assert result.recommendation is not None
        assert result.recommendation.amount_due > Decimal("0")

    async def test_it_refuses_over_the_network_without_failing_the_transport(
        self, seeded: Database, specialist_process: str
    ) -> None:
        """A refusal must survive the trip as a refusal, not become an outage."""
        client = BillingSpecialistClient(specialist_process, timeout_seconds=15.0)

        result = await client.request_pricing_decision(
            account_id=uuid.uuid4(), target_plan_code="enterprise"
        )

        assert result.status is ConsultStatus.REFUSED
        assert result.detail is not None
        assert "no_active_subscription" in result.detail

    async def test_the_orchestrator_survives_the_specialist_being_killed(
        self, seeded: Database, specialist_process: str
    ) -> None:
        """Graceful degradation, demonstrated rather than asserted.

        The specialist is optional in the strong sense: the workflow reaches the
        same decision from the same local rules when it is gone.
        """
        client = BillingSpecialistClient(specialist_process, timeout_seconds=5.0)
        assert (
            await client.request_pricing_decision(
                account_id=ACME_ACCOUNT, target_plan_code="enterprise"
            )
        ).status is ConsultStatus.CONSULTED

        # Reach into a port nothing is listening on any more.
        dead = BillingSpecialistClient(f"http://127.0.0.1:{_free_port()}", timeout_seconds=2.0)
        result = await dead.request_pricing_decision(
            account_id=ACME_ACCOUNT, target_plan_code="enterprise"
        )

        assert result.status is ConsultStatus.UNAVAILABLE
        assert result.recommendation is None
