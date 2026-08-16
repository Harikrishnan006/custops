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
from tests.integration.conftest import (
    FINANCE_EMAIL,
    OPERATOR_EMAIL,
    VIEWER_EMAIL,
    bearer,
    issue_test_token,
    requires_postgres,
    use_test_retrieval_policy,
)

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
async def app(seeded: Database, runtime_settings: Settings) -> AsyncIterator[FastAPI]:
    application: FastAPI = create_app(settings=runtime_settings)
    use_test_retrieval_policy(application)
    application.dependency_overrides[get_chat_provider] = lambda: _chat(NEEDS_APPROVAL)
    async with application.router.lifespan_context(application):
        yield application


async def _client_for(app: FastAPI, database: Database, email: str) -> AsyncClient:
    """A client authenticated as one seeded user (§17).

    Identity now comes from the token, so "who is deciding" is expressed by
    *which client* is used — not by a field in the request body.
    """
    token = await issue_test_token(database, email=email)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=bearer(token),
    )


@pytest.fixture
async def client(app: FastAPI, seeded: Database) -> AsyncIterator[AsyncClient]:
    """The ops user: holds `operator` and `approver`."""
    async with await _client_for(app, seeded, OPERATOR_EMAIL) as http:
        yield http


@pytest.fixture
async def finance(app: FastAPI, seeded: Database) -> AsyncIterator[AsyncClient]:
    """Elevated approval authority."""
    async with await _client_for(app, seeded, FINANCE_EMAIL) as http:
        yield http


@pytest.fixture
async def viewer(app: FastAPI, seeded: Database) -> AsyncIterator[AsyncClient]:
    """Read-only: may see approvals, may not decide them."""
    async with await _client_for(app, seeded, VIEWER_EMAIL) as http:
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
    async def test_an_approver_may_decide(
        self, client: AsyncClient, finance: AsyncClient
    ) -> None:
        """The seeded upgrade prices above the elevated threshold.

        `UMBRELLA`'s proration clears the policy's 10000.00 line, so the
        approver with authority over it is the finance one. The operator starts
        the run — deciding is a separate authority from requesting, which is the
        whole point of the split (§17, DIS-002).
        """
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        response = await finance.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True},
        )

        assert response.status_code == 200, response.text
        assert response.json()["approval"]["status"] == ApprovalStatus.APPROVED

    async def test_an_approver_below_the_amount_threshold_is_refused(
        self, client: AsyncClient, seeded: Database
    ) -> None:
        """The other half of the rule above, kept as its own case.

        The ops user holds `approver` and would be entitled to decide a routine
        upgrade. This one prices above the threshold, so authority is refused on
        the *amount* rather than on the role — and the record is left untouched
        for someone who does hold it.
        """
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        response = await client.post(
            f"/approvals/{approval_id}/decision", json={"approved": True}
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "elevated_role_required"

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.status == ApprovalStatus.PENDING
        assert approval.decided_by_user_id is None

    async def test_a_user_without_an_approving_role_is_refused(
        self, client: AsyncClient, viewer: AsyncClient
    ) -> None:
        """Refused at the endpoint now, before authority is even consulted.

        Phase 13 moved this one check earlier: a viewer no longer reaches the
        handler, so the denial is `insufficient_role` from the endpoint policy
        rather than `no_approval_role` from the amount policy. Both refuse; the
        earlier one refuses without touching the approval record at all.
        """
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        response = await viewer.post(
            f"/approvals/{approval_id}/decision", json={"approved": True}
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "insufficient_role"

    async def test_a_deactivated_approver_cannot_authenticate_at_all(
        self, app: FastAPI, client: AsyncClient, seeded: Database
    ) -> None:
        """A closed account is stopped one layer earlier than before.

        Previously this was a 403 on authority. Now a deactivated user cannot
        authenticate, so the credential is refused before any approval logic
        runs — and their existing tokens stop working without anyone having to
        remember which ones they hold.

        The token is inserted directly because `issue()` refuses to mint one for
        an inactive user; the row is what an already-issued credential would
        look like after the account was closed.
        """
        from custops.apps.api.security.tokens import mint
        from custops.domain.models.credential import ApiToken

        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        minted = mint()
        async with seeded.session_factory() as session:
            session.add(
                ApiToken(
                    token_hash=minted.token_hash,
                    label="former",
                    user_id=FORMER_APPROVER,
                )
            )
            await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=bearer(minted.plaintext),
        ) as former:
            response = await former.post(
                f"/approvals/{approval_id}/decision", json={"approved": True}
            )

        assert response.status_code == 401

    async def test_an_unknown_credential_is_refused(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """There is no longer any way to name an actor that does not exist.

        Identity comes from a token, so the equivalent attack is presenting a
        token nobody issued — refused at authentication with a 401.
        """
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=bearer("custops_not_a_real_token"),
        ) as impostor:
            response = await impostor.post(
                f"/approvals/{approval_id}/decision", json={"approved": True}
            )

        assert response.status_code == 401

    async def test_a_refused_decision_leaves_the_approval_pending(
        self, client: AsyncClient, viewer: AsyncClient, seeded: Database
    ) -> None:
        """An unauthorised attempt must not alter the record."""
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        await viewer.post(f"/approvals/{approval_id}/decision", json={"approved": True})

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.status == ApprovalStatus.PENDING
        assert approval.decided_by_user_id is None


class TestReplayAndReuse:
    async def test_an_approval_cannot_be_decided_twice(
        self, client: AsyncClient, finance: AsyncClient
    ) -> None:
        """Otherwise a rejection becomes an approval after the fact.

        Both attempts come from the approver who genuinely holds authority over
        this amount, so the second is refused for being a replay and not for
        want of a role — which is the property under test.
        """
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        body = {"approved": False}

        first = await finance.post(f"/approvals/{approval_id}/decision", json=body)
        second = await finance.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True},
        )

        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "already_decided"

    async def test_the_original_decision_survives_a_second_attempt(
        self, client: AsyncClient, finance: AsyncClient, seeded: Database
    ) -> None:
        """A settled decision is not reopened by a later attempt.

        Where the sibling test asserts the *status code* of the replay, this one
        asserts the *record*: a refused second attempt must leave the original
        verdict and its actor exactly as they were.

        A note on what this can no longer show. It used to have a junior
        approver decide and the finance approver try to overturn, so that
        seniority could not reopen a settled decision. Above the 10000.00
        threshold the finance approver is the only seeded actor with authority
        at all — there is no `admin` user — so that ordering is not expressible
        here without lowering the amount or inventing a role. The replay is
        therefore made by the same authorised approver, which still proves the
        decision is closed rather than merely guarded by a role check.
        """
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        await finance.post(f"/approvals/{approval_id}/decision", json={"approved": False})
        await finance.post(f"/approvals/{approval_id}/decision", json={"approved": True})

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.status == ApprovalStatus.REJECTED
        assert approval.decided_by_user_id == FINANCE_APPROVER

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
            json={"approved": True},
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
    async def test_approving_resumes_the_workflow(
        self, client: AsyncClient, finance: AsyncClient
    ) -> None:
        """Reuses the graph's own interrupt/resume, not a second mechanism."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]

        decision = (
            await finance.post(
                f"/approvals/{approval_id}/decision",
                json={"approved": True},
            )
        ).json()

        assert decision["workflow_resumed"] is True
        assert decision["workflow_status"] != WorkflowStatus.AWAITING_APPROVAL

    async def test_rejecting_escalates_without_executing(
        self, client: AsyncClient, finance: AsyncClient
    ) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        execution_id = started["execution_id"]

        await finance.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": False},
        )
        trace = (await client.get(f"/workflows/{execution_id}")).json()

        assert trace["status"] == WorkflowStatus.ESCALATED
        assert "execute" not in [step["node"] for step in trace["steps"]]

    async def test_the_graph_reads_the_decision_from_the_record(
        self, client: AsyncClient, finance: AsyncClient, seeded: Database
    ) -> None:
        """One authority on what a human decided.

        The API writes the row; the node reads it back. If the node also wrote,
        the two views could diverge and the later write would win.
        """
        started = await _pause_a_workflow(client)
        approval_id = uuid.UUID(started["awaiting_approval"]["approval_id"])

        await finance.post(
            f"/approvals/{approval_id}/decision",
            json={
                "approved": True,
                "note": "Checked the discount against DIS-002.",
            },
        )

        async with seeded.session_factory() as session:
            approval = await session.get(Approval, approval_id)

        assert approval is not None
        assert approval.decided_by_user_id == FINANCE_APPROVER
        assert approval.decision_note == "Checked the discount against DIS-002."
        assert approval.decided_at is not None


class TestAuditability:
    async def test_the_decision_is_audited_with_its_actor(
        self, client: AsyncClient, finance: AsyncClient, seeded: Database
    ) -> None:
        """§13: actor and timestamp. An approval trail without an actor is not one."""
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        execution_id = uuid.UUID(started["execution_id"])

        await finance.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True},
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
        assert events[0].actor_id == str(FINANCE_APPROVER)

    async def test_the_decision_appears_in_the_workflow_trace(
        self, client: AsyncClient, finance: AsyncClient
    ) -> None:
        started = await _pause_a_workflow(client)
        approval_id = started["awaiting_approval"]["approval_id"]
        execution_id = started["execution_id"]

        await finance.post(
            f"/approvals/{approval_id}/decision",
            json={"approved": True},
        )
        trace = (await client.get(f"/workflows/{execution_id}")).json()

        assert "approval_received" in [e["event_type"] for e in trace["audit_events"]]


class TestNotFound:
    async def test_deciding_an_unknown_approval_is_a_404(self, client: AsyncClient) -> None:
        response = await client.post(
            f"/approvals/{uuid.uuid4()}/decision",
            json={"approved": True},
        )

        assert response.status_code == 404
