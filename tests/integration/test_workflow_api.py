"""The Subscription Upgrade workflow, end to end over HTTP (Phase 6).

Drives the real API with a real database, a real graph, real MCP tools and the
PostgreSQL checkpointer. The model is the deterministic stand-in, so a failure
here is a system regression rather than model drift.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
from custops.domain.models.knowledge import EMBEDDING_DIMENSIONS
from custops.domain.models.workflow import WorkflowExecution, WorkflowStep
from custops.domain.seed import clear_seed_data, seed_all
from custops.knowledge.ingestion.pipeline import ingest_contracts, ingest_policies
from custops.providers.chat import DeterministicChatProvider
from custops.providers.deterministic import DeterministicEmbeddingProvider
from tests.integration.conftest import (
    OPERATOR_EMAIL,
    bearer,
    issue_test_token,
    requires_postgres,
)

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
EMBEDDER = DeterministicEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)


def _chat(customer_ref: str = "ACME", plan: str = "enterprise") -> DeterministicChatProvider:
    chat = DeterministicChatProvider()
    chat.register(
        RequestClassification,
        RequestClassification(
            workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE,
            customer_ref=customer_ref,
            target_plan_code=plan,
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
    """The real app, with only the model swapped for the deterministic double.

    Authenticated as the seeded operator, through the same dependency
    production uses (§17). There is no authentication-disabled test mode.
    """
    app: FastAPI = create_app(settings=runtime_settings)
    app.dependency_overrides[get_chat_provider] = lambda: _chat()

    token = await issue_test_token(seeded, email=OPERATOR_EMAIL)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=bearer(token),
        ) as http_client,
    ):
        yield http_client


class TestStartingAWorkflow:
    async def test_a_run_is_accepted_and_returns_an_execution_id(self, client: AsyncClient) -> None:
        response = await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})

        assert response.status_code in (201, 202), response.text
        body = response.json()
        assert uuid.UUID(body["execution_id"])
        assert body["workflow_type"] == WorkflowType.SUBSCRIPTION_UPGRADE

    async def test_the_run_reaches_validation_and_reports_the_divergence(
        self, client: AsyncClient
    ) -> None:
        """The D8 failure, surfaced through the API.

        Billing and the CRM accept the change; nothing provisions the
        entitlement until Phase 8, so the workflow refuses to report success.
        """
        response = await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        body = response.json()

        verdicts = {r["check"]: r["verdict"] for r in body["validation_results"]}
        assert verdicts.get("entitlement_tier") == "fail"
        assert body["status"] != WorkflowStatus.COMPLETED

    async def test_evidence_is_returned_as_citations_only(self, client: AsyncClient) -> None:
        """Rule 18 and §6: references, never retrieved prose."""
        response = await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        body = response.json()

        assert body["evidence_citations"]
        assert all(isinstance(c, str) for c in body["evidence_citations"])

    async def test_an_empty_request_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/workflows", json={"request": ""})

        assert response.status_code == 422


class TestTraceReconstruction:
    async def test_a_full_trace_is_reconstructable_from_the_execution_id(
        self, client: AsyncClient
    ) -> None:
        """§16: one id joins the graph steps, tool calls and audit events."""
        started = (
            await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        ).json()
        execution_id = started["execution_id"]

        response = await client.get(f"/workflows/{execution_id}")

        assert response.status_code == 200, response.text
        trace = response.json()
        assert trace["execution_id"] == execution_id
        assert trace["raw_request"] == "Upgrade Acme to Enterprise."
        assert trace["steps"], "no graph steps recorded"
        assert trace["tool_calls"], "no tool calls recorded"
        assert trace["audit_events"], "no audit events recorded"

    async def test_steps_are_ordered_and_name_the_nodes_visited(self, client: AsyncClient) -> None:
        started = (
            await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        ).json()

        trace = (await client.get(f"/workflows/{started['execution_id']}")).json()

        sequences = [step["sequence"] for step in trace["steps"]]
        assert sequences == sorted(sequences)
        nodes = [step["node"] for step in trace["steps"]]
        assert "supervisor" in nodes
        assert "research" in nodes

    async def test_tool_calls_were_written_by_the_tool_layer(self, client: AsyncClient) -> None:
        """The trace joins records written by *different* layers under one id."""
        started = (
            await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        ).json()

        trace = (await client.get(f"/workflows/{started['execution_id']}")).json()

        names = {call["tool_name"] for call in trace["tool_calls"]}
        assert "get_subscription" in names

    async def test_the_final_state_carries_citations_not_content(self, client: AsyncClient) -> None:
        started = (
            await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        ).json()

        trace = (await client.get(f"/workflows/{started['execution_id']}")).json()

        assert set(trace["final_state"]) == {
            "workflow_type",
            "decisions",
            "evidence_citations",
            "validation_results",
            "execution_results",
            "errors",
            "approval_status",
            "metadata",
        }

    async def test_an_unknown_execution_id_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get(f"/workflows/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_runs_are_listed_newest_first(self, client: AsyncClient) -> None:
        await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        await client.post("/workflows", json={"request": "Upgrade Hooli to Enterprise."})

        listing = (await client.get("/workflows", params={"limit": 5})).json()

        assert len(listing) >= 2
        timestamps = [item["started_at"] for item in listing]
        assert timestamps == sorted(timestamps, reverse=True)


class TestPersistence:
    async def test_the_execution_row_records_budgets_and_outcome(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        started = (
            await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        ).json()

        async with seeded.session_factory() as session:
            execution = await session.get(WorkflowExecution, uuid.UUID(started["execution_id"]))

        assert execution is not None
        assert execution.customer_ref == "ACME"
        assert execution.target_plan_code == "enterprise"
        assert execution.retry_count >= 0

    async def test_each_node_visit_is_its_own_row(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """Per visit, not per node — a retry loop must be visible."""
        started = (
            await client.post("/workflows", json={"request": "Upgrade Acme to Enterprise."})
        ).json()

        async with seeded.session_factory() as session:
            steps = list(
                (
                    await session.execute(
                        select(WorkflowStep)
                        .where(WorkflowStep.execution_id == uuid.UUID(started["execution_id"]))
                        .order_by(WorkflowStep.sequence)
                    )
                ).scalars()
            )

        assert steps
        assert [s.sequence for s in steps] == list(range(len(steps)))


class TestApprovalPause:
    async def test_a_run_requiring_approval_pauses_with_202_and_a_prompt(
        self, seeded: Database, runtime_settings: Settings
    ) -> None:
        """Umbrella's 35% discount trips the approval threshold (§13)."""
        app = create_app(settings=runtime_settings)
        app.dependency_overrides[get_chat_provider] = lambda: _chat(customer_ref="UMBRELLA")

        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=bearer(await issue_test_token(seeded, email=OPERATOR_EMAIL)),
            ) as client,
        ):
            response = await client.post(
                "/workflows", json={"request": "Upgrade Umbrella to Enterprise."}
            )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == WorkflowStatus.AWAITING_APPROVAL
        prompt = body["awaiting_approval"]
        assert prompt is not None
        assert prompt["action"] == "subscription_upgrade"
        assert prompt["reasons"]

    async def test_a_paused_run_has_no_finished_at(
        self, seeded: Database, runtime_settings: Settings
    ) -> None:
        """Otherwise a paused run is indistinguishable from a completed one."""
        app = create_app(settings=runtime_settings)
        app.dependency_overrides[get_chat_provider] = lambda: _chat(customer_ref="UMBRELLA")

        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=bearer(await issue_test_token(seeded, email=OPERATOR_EMAIL)),
            ) as client,
        ):
            started = (
                await client.post("/workflows", json={"request": "Upgrade Umbrella to Enterprise."})
            ).json()

        async with seeded.session_factory() as session:
            execution = await session.get(WorkflowExecution, uuid.UUID(started["execution_id"]))

        assert execution is not None
        assert execution.finished_at is None
        assert execution.status == WorkflowStatus.AWAITING_APPROVAL


class TestEscalation:
    async def test_a_blocked_contract_escalates_without_executing(
        self, seeded: Database, runtime_settings: Settings
    ) -> None:
        """Globex's term-locked contract stops the run before any mutation."""
        app = create_app(settings=runtime_settings)
        app.dependency_overrides[get_chat_provider] = lambda: _chat(customer_ref="GLOBEX")

        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=bearer(await issue_test_token(seeded, email=OPERATOR_EMAIL)),
            ) as client,
        ):
            started = (
                await client.post("/workflows", json={"request": "Upgrade Globex to Enterprise."})
            ).json()
            trace = (await client.get(f"/workflows/{started['execution_id']}")).json()

        assert started["status"] == WorkflowStatus.ESCALATED
        assert "contract_term_locked" in (started["escalation_reason"] or "")
        assert "execute" not in [step["node"] for step in trace["steps"]]
