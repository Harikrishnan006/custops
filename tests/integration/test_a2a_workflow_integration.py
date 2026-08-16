"""How the workflow uses the specialist, and how it copes without one.

A canned server behind the *real* client: the code under test is the
orchestrator's ``decide`` node, and the specialist's answer has to be steerable
in ways a live agent reading correct data cannot be made to produce — a
divergent figure, in particular, is the case that matters most and the one a
correct specialist will never generate.

The out-of-process claim is proven separately, in
``test_a2a_billing_specialist.py``, by starting the agent as a real subprocess.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from custops.a2a.client.billing import BillingSpecialistClient
from custops.a2a.contracts.pricing import CAPABILITY_PRICING_DECISION
from custops.agents.nodes import NodeDependencies, build_nodes
from custops.agents.state import ApprovalState, WorkflowStatus, initial_state
from custops.apps.enterprise import assessment as assessment_module
from custops.db.engine import Database
from custops.domain.models.knowledge import EMBEDDING_DIMENSIONS
from custops.domain.seed import clear_seed_data, seed_all, seed_id
from custops.providers.chat import DeterministicChatProvider
from custops.providers.deterministic import DeterministicEmbeddingProvider
from tests.integration.conftest import TEST_RETRIEVAL_POLICY, requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
ACME_ACCOUNT = seed_id("account", "acme")


@pytest.fixture
async def seeded(database: Database) -> AsyncIterator[Database]:
    async with database.session_factory() as session:
        await seed_all(session, now=NOW)
        await session.commit()
    try:
        yield database
    finally:
        async with database.session_factory() as session:
            await clear_seed_data(session)
            await session.commit()


def _consulting_deps(database: Database, transport: httpx.MockTransport | None) -> NodeDependencies:
    return NodeDependencies(
        session_factory=database.session_factory,
        chat=DeterministicChatProvider(),
        embedder=DeterministicEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS),
        billing_specialist=(
            BillingSpecialistClient("http://specialist.test", transport=transport)
            if transport is not None
            else None
        ),
        retrieval_policy=TEST_RETRIEVAL_POLICY,
        clock=lambda: NOW,
    )


def _decide_state() -> dict[str, Any]:
    state = dict(
        initial_state(
            execution_id=uuid.uuid4(),
            request_id="req-a2a-decide",
            raw_request="Upgrade ACME to enterprise.",
            started_at=NOW,
        )
    )
    state["account_id"] = ACME_ACCOUNT
    state["target_plan_code"] = "enterprise"
    return state


def _task_returning(amount: str, *, approval_indicated: bool = False) -> httpx.MockTransport:
    """A specialist that answers with a chosen figure."""
    data = {
        "account_id": str(ACME_ACCOUNT),
        "current_plan_code": "professional",
        "target_plan_code": "enterprise",
        "amount_due": amount,
        "currency": "USD",
        "unused_credit": "0.00",
        "new_plan_charge": amount,
        "days_remaining": 15,
        "days_in_period": 30,
        "billing_eligible": True,
        "billing_blockers": [],
        "approval_indicated": approval_indicated,
        "approval_triggers": ["amount_threshold"] if approval_indicated else [],
        "confidence": 0.9,
        "rationale_summary": "Prorated.",
        "evidence_refs": [],
    }
    task = {
        "id": str(uuid.uuid4()),
        "contextId": str(uuid.uuid4()),
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": CAPABILITY_PRICING_DECISION,
                "parts": [{"data": data}],
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=task)

    return httpx.MockTransport(handler)


def _unreachable() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return httpx.MockTransport(handler)


async def _local_amount(database: Database) -> Decimal:
    async with database.session_factory() as session:
        assessment = await assessment_module.assess_upgrade(
            session, account_id=ACME_ACCOUNT, target_plan_code="enterprise", now=NOW
        )
        await session.commit()
    return assessment.proration.amount_due


class TestWorkflowIntegration:
    async def test_agreement_is_recorded_and_changes_nothing(self, seeded: Database) -> None:
        amount = await _local_amount(seeded)
        nodes = build_nodes(_consulting_deps(seeded, _task_returning(str(amount))))

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        trace = update["metadata"]["billing_specialist"]
        assert trace["status"] == "consulted"
        assert trace["agrees"] is True
        assert "specialist_amount_divergence" not in update["metadata"]["approval_triggers"]

    async def test_a_divergent_figure_forces_human_review(self, seeded: Database) -> None:
        """Two systems pricing the same account differently is a review case.

        One of them is reading stale or wrong state, and neither side can tell
        which from here — so the workflow stops for a human rather than picking.
        """
        nodes = build_nodes(_consulting_deps(seeded, _task_returning("99999.00")))

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        assert update["approval_status"] == ApprovalState.REQUIRED
        assert update["status"] == WorkflowStatus.AWAITING_APPROVAL
        assert "specialist_amount_divergence" in update["metadata"]["approval_triggers"]
        assert update["metadata"]["billing_specialist"]["agrees"] is False

    async def test_the_local_figure_still_governs_when_they_disagree(
        self, seeded: Database
    ) -> None:
        """The specialist is corroboration, not an authority.

        An out-of-process agent that could change the amount charged would move
        the money decision outside the audited deterministic path.
        """
        amount = await _local_amount(seeded)
        nodes = build_nodes(_consulting_deps(seeded, _task_returning("99999.00")))

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        assert update["metadata"]["proration_amount"] == str(amount)
        pricing = next(d for d in update["decisions"] if d["name"] == "pricing")
        assert pricing["outcome"] == str(amount)

    async def test_the_specialist_may_raise_the_approval_bar(self, seeded: Database) -> None:
        """It reads a subset of the data, so it may only ever add a gate.

        Its 'no approval needed' is uninformed rather than reassuring — and a
        remote agent able to clear a human gate would put the approval decision
        outside the audited local path entirely (D9).
        """
        amount = await _local_amount(seeded)
        nodes = build_nodes(
            _consulting_deps(seeded, _task_returning(str(amount), approval_indicated=True))
        )

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        assert update["approval_status"] == ApprovalState.REQUIRED
        assert "specialist_indicated_approval" in update["metadata"]["approval_triggers"]

    async def test_the_workflow_proceeds_when_the_specialist_is_unreachable(
        self, seeded: Database
    ) -> None:
        """Graceful degradation: same local decision, plus a record of the gap."""
        amount = await _local_amount(seeded)
        nodes = build_nodes(_consulting_deps(seeded, _unreachable()))

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        assert update["metadata"]["proration_amount"] == str(amount)
        assert update["metadata"]["billing_specialist"]["status"] == "unavailable"
        assert "specialist_amount_divergence" not in update["metadata"]["approval_triggers"]

    async def test_an_unconfigured_specialist_leaves_the_workflow_unchanged(
        self, seeded: Database
    ) -> None:
        """No specialist configured is the default, and must be a no-op."""
        amount = await _local_amount(seeded)
        nodes = build_nodes(_consulting_deps(seeded, None))

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        assert update["metadata"]["proration_amount"] == str(amount)
        assert update["metadata"]["billing_specialist"]["status"] == "unavailable"
        # No consultation was attempted, so none is recorded as a decision.
        assert not [
            d for d in update["decisions"] if d["name"] == "billing_specialist_consultation"
        ]

    async def test_the_consultation_is_recorded_without_reasoning(self, seeded: Database) -> None:
        """Rule 18 applies across the process boundary too."""
        amount = await _local_amount(seeded)
        nodes = build_nodes(_consulting_deps(seeded, _task_returning(str(amount))))

        update = await nodes.decide(_decide_state())  # type: ignore[arg-type]

        decision = next(
            d for d in update["decisions"] if d["name"] == "billing_specialist_consultation"
        )
        assert decision["outcome"] == "consulted"
        assert len(decision["rationale_summary"]) < 500
        assert set(decision) == {
            "name",
            "outcome",
            "confidence",
            "rationale_summary",
            "evidence_refs",
            "decided_at",
        }
