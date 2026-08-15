"""The approval API and the full three-layer loop (§13).

Phase 4 already proves layer 3 in isolation
(``test_approval_enforcement.py``: call a mutating tool directly, bypassing the
graph, and it refuses). This file proves the loop those layers form together —
a workflow pauses, a human decides through the API, the graph resumes, and the
tool layer independently re-verifies before acting.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from custops.agents.schemas import NotificationDraft, PlanDraft, RequestClassification
from custops.agents.state import WorkflowStatus, WorkflowType
from custops.apps.api.main import create_app
from custops.apps.api.routers.workflows import get_chat_provider
from custops.config import Settings
from custops.db.engine import Database
from custops.domain.models.approval import Approval, ApprovalStatus
from custops.domain.models.audit import AuditEvent
from custops.domain.models.knowledge import EMBEDDING_DIMENSIONS
from custops.domain.seed import clear_seed_data, seed_all, seed_id
from custops.knowledge.ingestion.pipeline import ingest_contracts, ingest_policies
from custops.providers.chat import DeterministicChatProvider
from custops.providers.deterministic import DeterministicEmbeddingProvider
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
EMBEDDER = DeterministicEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)

# Seeded actors, each exercising a different authority path.
OPS_APPROVER = seed_id("user", "ops")
FINANCE_APPROVER = seed_id("user", "finance")
VIEWER = seed_id("user", "viewer")
FORMER_APPROVER = seed_id("user", "former")

# Umbrella's 35% discount trips the approval threshold (§13).
NEEDS_APPROVAL = "UMBRELLA"


def _chat(customer_ref: str) -> DeterministicChatProvider:
    chat = DeterministicChatProvider()
    chat.register(
        RequestClassification,
        RequestClassification(
            workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE,
            customer_ref=customer_ref,
            target_plan_code="enterprise",
            confidence=0.95,
            rationale_summary="Explicit upgrade request.",
        ),
    )
    chat.register(PlanDraft, PlanDraft(workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE))
    chat.register(NotificationDraft, NotificationDraft(subject="Upgraded", body="Done."))
    return chat


@pytest.fixture
async def seeded(database: Database) -> AsyncIterator[Database]:
    async with database.session_factory() as session:
        await seed_all(session, now=NOW)
        await ingest_policies(session, EMBEDDER, now=NOW)
        await ingest_contracts(session, EMBEDDER, now=NOW)
        await session.commit()
    try:
        yield database
    finally:
        async with database.session_factory() as session:
            await clear_seed_data(session)
            await session.commit()


@pytest.fixture
async def client(seeded: Database, runtime_settings: Settings) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings=runtime_settings)
    app.dependency_overrides[get_chat_provider] = lambda: _chat(NEEDS_APPROVAL)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http,
    ):
        yield http


async def _pause_a_workflow(client: AsyncClient) -> dict[str, Any]:
    """Start a run that stops at the approval gate."""
    response = await client.post(
        "/workflows", json={"request": f"Upgrade {NEEDS_APPROVAL} to Enterprise."}
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


class TestApprovalRequestContents:
    async def test_a_paused_run_creates_a_pending_approval(self, client: AsyncClient) -> None:
        await _pause_a_workflow(client)

        listing = (await client.get("/approvals", params={"status": "pending"})).json()

        assert listing
        assert all(item["status"] == ApprovalStatus.PENDING for item in listing)

    async def test_the_request_carries_everything_section_13_requires(
        self, client: AsyncClient
    ) -> None:
        """Entity, action, reason, evidence, risk assessment, expected outcome."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        approval = (await client.get(f"/approvals/{approval_id}")).json()

        assert approval["entity_type"] and approval["entity_id"]
        assert approval["action"] == "subscription_upgrade"
        assert approval["reason"]
        assert approval["expected_outcome"]
        assert approval["risk_assessment"]
        assert approval["evidence"]["citations"]

    async def test_pending_requests_are_listed_oldest_first(self, client: AsyncClient) -> None:
        await _pause_a_workflow(client)
        await _pause_a_workflow(client)

        listing = (await client.get("/approvals", params={"status": "pending"})).json()

        requested = [item["requested_at"] for item in listing]
        assert requested == sorted(requested)


class TestAuthorization:
    async def test_an_approver_may_decide(self, client: AsyncClient) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        response = await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
        )

        assert response.status_code == 200, response.text
        assert response.json()["approval"]["status"] == ApprovalStatus.APPROVED

    async def test_a_user_without_an_approving_role_is_refused(self, client: AsyncClient) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        response = await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(VIEWER)},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "no_approval_role"

    async def test_a_deactivated_approver_is_refused(self, client: AsyncClient) -> None:
        """A role alone must not confer authority on a closed account."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        response = await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(FORMER_APPROVER)},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "actor_inactive"

    async def test_an_unknown_actor_is_refused(self, client: AsyncClient) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        response = await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(uuid.uuid4())},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "actor_not_found"

    async def test_a_refused_decision_leaves_the_approval_pending(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """An unauthorised attempt must not alter the record."""
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(VIEWER)},
        )

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.status == ApprovalStatus.PENDING
        assert approval.decided_by_user_id is None


class TestReplayAndReuse:
    async def test_an_approval_cannot_be_decided_twice(self, client: AsyncClient) -> None:
        """Otherwise a rejection becomes an approval after the fact."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        body = {"approved": False, "actor_user_id": str(OPS_APPROVER)}

        first = await client.post(f"/approvals/{approval_id}/decision", json=body)
        second = await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
        )

        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "already_decided"

    async def test_the_original_decision_survives_a_second_attempt(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": False, "actor_user_id": str(OPS_APPROVER)},
        )
        await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(FINANCE_APPROVER)},
        )

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.status == ApprovalStatus.REJECTED
        assert approval.decided_by_user_id == OPS_APPROVER

    async def test_a_consumed_approval_cannot_be_re_decided(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """Layer 3 marks it spent; layer 2 must respect that."""
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)
            assert approval is not None
            approval.consumed_at = datetime.now(UTC)
            await session.commit()

        response = await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "already_consumed"

    async def test_a_stale_approval_is_refused_by_the_tool_layer(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """Approve today, execute next month is a replay in slow motion.

        Layer 3 checks freshness for itself rather than trusting a sweeper to
        have flipped the status (D9).
        """
        from custops.mcp.tools.approval import ApprovalRequirement, verify_approval
        from custops.mcp.tools.results import ToolExecutionError

        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)
            assert approval is not None
            approval.status = ApprovalStatus.APPROVED
            approval.decided_at = datetime.now(UTC) - timedelta(days=30)
            approval.consumed_at = None
            await session.commit()

            with pytest.raises(ToolExecutionError) as error:
                await verify_approval(
                    session,
                    ApprovalRequirement(
                        execution_id=approval.execution_id,
                        action=approval.action,
                        entity_type=approval.entity_type,
                        entity_id=approval.entity_id,
                    ),
                )

        assert "no longer current" in error.value.message


class TestResumeLoop:
    async def test_approving_resumes_the_workflow(self, client: AsyncClient) -> None:
        """Reuses the graph's own interrupt/resume, not a second mechanism."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        decision = (
            await client.post(
                f"/approvals/{approval_id}/decision",
                json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
            )
        ).json()

        assert decision["workflow_resumed"] is True
        assert decision["workflow_status"] != WorkflowStatus.AWAITING_APPROVAL

    async def test_rejecting_escalates_without_executing(self, client: AsyncClient) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        execution_id = started["execution_id"]

        await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": False, "actor_user_id": str(OPS_APPROVER)},
        )
        trace = (await client.get(f"/workflows/{execution_id}")).json()

        assert trace["status"] == WorkflowStatus.ESCALATED
        assert "execute" not in [step["node"] for step in trace["steps"]]

    async def test_the_graph_reads_the_decision_from_the_record(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """One authority on what a human decided.

        The API writes the row; the node reads it back. If the node also wrote,
        the two views could diverge and the later write would win.
        """
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        await client.post(
            f"/approvals/{approval_id}/decision",
            json={
                "approved": True,
                "actor_user_id": str(OPS_APPROVER),
                "note": "Checked the discount against DIS-002.",
            },
        )

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.decided_by_user_id == OPS_APPROVER
        assert approval.decision_note == "Checked the discount against DIS-002."
        assert approval.decided_at is not None


class TestAuditability:
    async def test_the_decision_is_audited_with_its_actor(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """§13: actor and timestamp. An approval trail without an actor is not one."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        execution_id = uuid.UUID(started["execution_id"])

        await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
        )

        async with seeded.session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(AuditEvent).where(
                            AuditEvent.execution_id == execution_id,
                            AuditEvent.event_type == "approval_received",
                        )
                    )
                ).scalars()
            )

        assert events
        assert events[0].actor_type == "user"
        assert events[0].actor_id == str(OPS_APPROVER)

    async def test_the_decision_appears_in_the_workflow_trace(self, client: AsyncClient) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        execution_id = started["execution_id"]

        await client.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
        )
        trace = (await client.get(f"/workflows/{execution_id}")).json()

        assert "approval_received" in [e["event_type"] for e in trace["audit_events"]]


class TestNotFound:
    async def test_deciding_an_unknown_approval_is_a_404(self, client: AsyncClient) -> None:
        response = await client.post(
            f"/approvals/{uuid.uuid4()}/decision",
            json={"approved": True, "actor_user_id": str(OPS_APPROVER)},
        )

        assert response.status_code == 404
