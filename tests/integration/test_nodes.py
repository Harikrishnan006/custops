"""Agent nodes against real systems of record.

The test that matters most here is
``test_a_successful_execution_still_fails_validation``: billing and the CRM both
accept the upgrade, and validation refuses to pass because nothing provisioned
the entitlement. That is decision D8 arriving on schedule — the Playwright step
that flips the portal is Phase 8 — and the workflow correctly reporting its own
incompleteness rather than declaring success.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from custops.agents.nodes import NodeDependencies, build_nodes
from custops.agents.schemas import NotificationDraft, PlanDraft, RequestClassification
from custops.agents.state import (
    ApprovalState,
    ValidationVerdict,
    WorkflowStatus,
    WorkflowType,
    initial_state,
)
from custops.agents.validation import overall_verdict
from custops.db.engine import Database
from custops.domain.models.approval import Approval, ApprovalStatus
from custops.domain.models.billing import Plan, Subscription
from custops.domain.models.knowledge import EMBEDDING_DIMENSIONS
from custops.domain.seed import seed_all, seed_id
from custops.knowledge.ingestion.pipeline import ingest_contracts, ingest_policies
from custops.providers.chat import DeterministicChatProvider
from custops.providers.deterministic import DeterministicEmbeddingProvider
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
EMBEDDER = DeterministicEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)


def _chat(customer_ref: str = "ACME", plan: str = "enterprise") -> DeterministicChatProvider:
    """A model that always classifies the same way.

    Fixed so a failing node test means the node changed, not that a model did.
    """
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
    chat.register(NotificationDraft, NotificationDraft(subject="Upgrade confirmed", body="Done."))
    return chat


@pytest.fixture
async def seeded(database: Database) -> AsyncIterator[Database]:
    """Seed and ingest, then remove it all afterwards.

    Committed rather than rolled back, because the nodes open their own
    sessions and would not see uncommitted data.
    """
    async with database.session_factory() as session:
        await seed_all(session, now=NOW)
        await ingest_policies(session, EMBEDDER, now=NOW)
        await ingest_contracts(session, EMBEDDER, now=NOW)
        await session.commit()
    try:
        yield database
    finally:
        async with database.session_factory() as session:
            from custops.domain.seed import clear_seed_data

            await clear_seed_data(session)
            await session.commit()


def _deps(database: Database, chat: DeterministicChatProvider | None = None) -> NodeDependencies:
    return NodeDependencies(
        session_factory=database.session_factory,
        chat=chat or _chat(),
        embedder=EMBEDDER,
        clock=lambda: NOW,
    )


def _state(customer_ref: str = "ACME", plan: str = "enterprise") -> dict[str, Any]:
    state = initial_state(
        execution_id=uuid.uuid4(),
        request_id="req-node-test",
        raw_request=f"Upgrade {customer_ref} to {plan}.",
        started_at=NOW,
    )
    return dict(state)


class TestSupervisorAndPlanner:
    async def test_supervisor_classifies_and_records_a_decision(self, seeded: Database) -> None:
        nodes = build_nodes(_deps(seeded))

        update = await nodes.supervisor(_state())  # type: ignore[arg-type]

        assert update["workflow_type"] == WorkflowType.SUBSCRIPTION_UPGRADE
        assert update["customer_ref"] == "ACME"
        assert update["decisions"][0]["name"] == "request_classification"

    async def test_no_chain_of_thought_is_stored(self, seeded: Database) -> None:
        """Rule 18: a conclusion, not the reasoning that produced it."""
        nodes = build_nodes(_deps(seeded))

        update = await nodes.supervisor(_state())  # type: ignore[arg-type]

        decision = update["decisions"][0]
        assert set(decision) == {
            "name",
            "outcome",
            "confidence",
            "rationale_summary",
            "evidence_refs",
            "decided_at",
        }
        assert len(decision["rationale_summary"]) < 500


class TestResearch:
    async def test_evidence_is_gathered_with_source_references(self, seeded: Database) -> None:
        nodes = build_nodes(_deps(seeded))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]

        update = await nodes.research(state)  # type: ignore[arg-type]

        assert update["account_id"] == seed_id("account", "acme")
        assert update["evidence"]
        assert all(item.get("source_ref") for item in update["evidence"])

    async def test_an_unknown_customer_produces_insufficient_evidence(
        self, seeded: Database
    ) -> None:
        nodes = build_nodes(_deps(seeded, chat=_chat(customer_ref="NOSUCHCO")))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]

        update = await nodes.research(state)  # type: ignore[arg-type]

        assert update["metadata"]["evidence_sufficient"] is False
        assert update["errors"]


class TestDecide:
    async def test_a_healthy_account_is_eligible_without_approval(self, seeded: Database) -> None:
        nodes = build_nodes(_deps(seeded))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]

        update = await nodes.decide(state)  # type: ignore[arg-type]

        assert update["approval_status"] == ApprovalState.NOT_REQUIRED
        assert any(d["name"] == "upgrade_eligibility" for d in update["decisions"])
        assert any(d["name"] == "pricing" for d in update["decisions"])

    async def test_a_term_locked_contract_escalates(self, seeded: Database) -> None:
        """The deterministic rule blocks; no model is consulted."""
        nodes = build_nodes(_deps(seeded, chat=_chat(customer_ref="GLOBEX")))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]

        update = await nodes.decide(state)  # type: ignore[arg-type]

        assert update["status"] == WorkflowStatus.ESCALATED
        assert "contract_term_locked" in update["escalation_reason"]

    async def test_a_deep_discount_requires_approval(self, seeded: Database) -> None:
        nodes = build_nodes(_deps(seeded, chat=_chat(customer_ref="UMBRELLA")))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]

        update = await nodes.decide(state)  # type: ignore[arg-type]

        assert update["approval_status"] == ApprovalState.REQUIRED
        assert update["metadata"]["approval_triggers"]


class TestExecuteAndValidate:
    async def test_execution_without_approval_is_refused_by_the_tool_layer(
        self, seeded: Database
    ) -> None:
        """D9 holds even when the caller is a graph node rather than a model."""
        nodes = build_nodes(_deps(seeded))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]
        state.update(await nodes.decide(state))  # type: ignore[arg-type]

        update = await nodes.execute(state)  # type: ignore[arg-type]

        assert any(not r["ok"] for r in update["execution_results"])
        assert any(e["code"] == "approval_required" for e in update["errors"])

    async def test_a_successful_execution_still_fails_validation(self, seeded: Database) -> None:
        """The D8 divergence, detected.

        Billing and the CRM both accept the change. Nothing flips the
        entitlement — that is Phase 8's Playwright step — so validation must
        refuse to pass rather than declare success from two agreeing systems.
        """
        nodes = build_nodes(_deps(seeded))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]
        state.update(await nodes.decide(state))  # type: ignore[arg-type]

        # Grant the approvals the tool layer requires, exactly as the approval
        # gate would.
        account_id = state["account_id"]
        async with seeded.session_factory() as session:
            subscription_id = (
                await session.execute(
                    select(Subscription.id).where(Subscription.account_id == account_id)
                )
            ).scalar_one()
            for entity_type, entity_id, action in (
                ("subscription", str(subscription_id), "subscription_upgrade"),
                ("account", str(account_id), "update_crm"),
            ):
                session.add(
                    Approval(
                        id=uuid.uuid4(),
                        execution_id=state["execution_id"],
                        action=action,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        status=ApprovalStatus.APPROVED,
                        reason="Granted for test.",
                        evidence={},
                    )
                )
            await session.commit()

        execution = await nodes.execute(state)  # type: ignore[arg-type]
        state.update(execution)
        validation = await nodes.validate(state)  # type: ignore[arg-type]

        assert all(r["ok"] for r in execution["execution_results"]), execution["errors"]

        results = validation["validation_results"]
        assert overall_verdict(results) == ValidationVerdict.FAIL

        entitlement = next(r for r in results if r["check"] == "entitlement_tier")
        billing = next(r for r in results if r["check"] == "subscription_plan")
        crm = next(r for r in results if r["check"] == "crm_plan_reference")

        assert billing["verdict"] == ValidationVerdict.PASS
        assert crm["verdict"] == ValidationVerdict.PASS
        assert entitlement["verdict"] == ValidationVerdict.FAIL
        assert entitlement["actual"] == "professional"

    async def test_the_divergence_is_recorded_as_non_retryable(self, seeded: Database) -> None:
        """Retrying cannot make two systems agree; only provisioning can."""
        nodes = build_nodes(_deps(seeded))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]
        state["target_plan_code"] = "enterprise"

        validation = await nodes.validate(state)  # type: ignore[arg-type]

        assert validation["errors"][0]["retryable"] is False
        assert "disagree" in validation["errors"][0]["message"]


class TestNotify:
    async def test_notification_records_that_delivery_is_unimplemented(
        self, seeded: Database
    ) -> None:
        """Rule 6: no node reports success for work no system performs."""
        nodes = build_nodes(_deps(seeded))

        update = await nodes.notify(_state())  # type: ignore[arg-type]

        detail = update["execution_results"][0]["detail"]
        assert detail["delivery"] == "not_implemented"


class TestPlanCatalogue:
    async def test_the_target_plan_exists(self, seeded: Database) -> None:
        """Guards the fixture: these tests are meaningless without the catalogue."""
        async with seeded.session_factory() as session:
            codes = set((await session.execute(select(Plan.code))).scalars())

        assert {"professional", "enterprise"} <= codes
