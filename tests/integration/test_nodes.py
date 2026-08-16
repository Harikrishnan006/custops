"""Agent nodes against real systems of record.

Two tests carry most of the weight, and they are a matched pair:

``test_a_fully_provisioned_execution_passes_validation`` runs the whole chain —
Billing → CRM → Legacy Portal → validation — and expects agreement. That is what
Phase 8 makes possible.

``test_a_portal_that_provisions_the_wrong_tier_is_caught`` forces the divergence
D8 exists for: every step reports success, the portal provisioned something
else, and validation must fail anyway. A system that only ever agrees with
itself has not been shown to detect anything.

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
from custops.provisioning.client import StubProvisioningClient
from tests.integration.conftest import TEST_RETRIEVAL_POLICY, requires_postgres

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


def _deps(
    database: Database,
    chat: DeterministicChatProvider | None = None,
    provisioning: StubProvisioningClient | None = None,
) -> NodeDependencies:
    return NodeDependencies(
        session_factory=database.session_factory,
        chat=chat or _chat(),
        embedder=EMBEDDER,
        provisioning=provisioning,
        retrieval_policy=TEST_RETRIEVAL_POLICY,
        clock=lambda: NOW,
    )


async def _grant_upgrade_approval(database: Database, state: dict[str, Any]) -> None:
    """Record the single approval the execute step verifies against.

    One row, scoped to the account — the same shape the approval gate creates,
    and what every mutation in the workflow checks. Granting per-tool approvals
    here would test a scheme the system does not use.
    """
    async with database.session_factory() as session:
        session.add(
            Approval(
                id=uuid.uuid4(),
                execution_id=state["execution_id"],
                action="subscription_upgrade",
                entity_type="account",
                entity_id=str(state["account_id"]),
                status=ApprovalStatus.APPROVED,
                reason="Granted for test.",
                evidence={},
                decided_at=datetime.now(UTC),
            )
        )
        await session.commit()


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

    async def test_execution_without_provisioning_fails_validation(self, seeded: Database) -> None:
        """The D8 divergence, detected.

        With no provisioning client configured, billing and the CRM accept the
        change and nothing provisions the entitlement. Validation must refuse to
        pass rather than declare success from two agreeing systems.

        Phase 8 added the provisioning step, so this is now the *unconfigured*
        path rather than the only path — see
        ``test_a_fully_provisioned_execution_passes_validation`` for the full
        Billing → CRM → Portal → validation chain.
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
        # Not consulted, because no provisioning client was configured — which
        # is reported as needing review rather than as agreement.
        assert entitlement["verdict"] == ValidationVerdict.NEEDS_REVIEW

    async def test_a_fully_provisioned_execution_passes_validation(self, seeded: Database) -> None:
        """Billing → CRM → Legacy Portal → validation, all agreeing.

        The point of Phase 8: an upgrade that genuinely provisions no longer
        fails merely because provisioning was missing.
        """
        provisioning = StubProvisioningClient()
        nodes = build_nodes(_deps(seeded, provisioning=provisioning))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]
        state.update(await nodes.decide(state))  # type: ignore[arg-type]

        await _grant_upgrade_approval(seeded, state)

        execution = await nodes.execute(state)  # type: ignore[arg-type]
        state.update(execution)
        validation = await nodes.validate(state)  # type: ignore[arg-type]

        assert all(r["ok"] for r in execution["execution_results"]), execution["errors"]
        assert overall_verdict(validation["validation_results"]) == ValidationVerdict.PASS
        assert any(call["op"] == "set_tier" for call in provisioning.calls)

    async def test_a_portal_that_provisions_the_wrong_tier_is_caught(
        self, seeded: Database
    ) -> None:
        """§11 asks for a test that forces exactly this divergence.

        Every step reports success and the portal provisioned something else.
        Validation must fail.
        """
        provisioning = StubProvisioningClient(drift_to="starter")
        nodes = build_nodes(_deps(seeded, provisioning=provisioning))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]
        state.update(await nodes.decide(state))  # type: ignore[arg-type]

        await _grant_upgrade_approval(seeded, state)

        state.update(await nodes.execute(state))  # type: ignore[arg-type]
        validation = await nodes.validate(state)  # type: ignore[arg-type]

        results = validation["validation_results"]
        entitlement = next(r for r in results if r["check"] == "entitlement_tier")

        assert overall_verdict(results) == ValidationVerdict.FAIL
        assert entitlement["actual"] == "starter"

    async def test_the_provisioning_step_requires_the_same_approval(self, seeded: Database) -> None:
        """D9 holds for the browser step too — no approval, no provisioning."""
        provisioning = StubProvisioningClient()
        nodes = build_nodes(_deps(seeded, provisioning=provisioning))
        state = _state()
        state.update(await nodes.supervisor(state))  # type: ignore[arg-type]
        state.update(await nodes.research(state))  # type: ignore[arg-type]
        state.update(await nodes.decide(state))  # type: ignore[arg-type]

        # No approval granted.
        execution = await nodes.execute(state)  # type: ignore[arg-type]

        assert any(e["code"] == "approval_required" for e in execution["errors"])
        assert not provisioning.calls, "the portal was driven without an approval"

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
