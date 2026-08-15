"""Decision D9: the tool layer enforces approval, independently.

The central test in this file calls a mutating tool **directly** — no graph, no
planner, no approval gate anywhere in the call stack — and asserts it is
refused. That is the whole point of the design: the graph is a happy path an LLM
can route around; the tool is a boundary it cannot.

BUILD_SPEC §13 asks for exactly this test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.db.engine import Database
from custops.domain.models.approval import Approval, ApprovalStatus, ToolCall
from custops.domain.models.audit import AuditEvent
from custops.domain.models.billing import Subscription
from custops.domain.seed import seed_all, seed_id
from custops.mcp.permissions.matrix import Role, ToolName
from custops.mcp.tools import enterprise as handlers
from custops.mcp.tools.results import ToolErrorCode
from custops.mcp.tools.runtime import ToolContext, execute_tool
from custops.mcp.tools.schemas import AccountInput, UpdateCrmInput, UpdateSubscriptionInput
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
EXECUTION_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as db_session:
        await seed_all(db_session, now=NOW)
        try:
            yield db_session
        finally:
            await db_session.rollback()


def _context(session: AsyncSession, role: str = Role.EXECUTION) -> ToolContext:
    return ToolContext(session=session, role=role, execution_id=EXECUTION_ID, request_id="req-test")


async def _grant(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    status: str = ApprovalStatus.APPROVED,
) -> Approval:
    approval = Approval(
        id=uuid.uuid4(),
        execution_id=EXECUTION_ID,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        status=status,
        reason="Integration test grant.",
        evidence={},
        decided_at=NOW,
    )
    session.add(approval)
    await session.flush()
    return approval


async def _subscription_id(session: AsyncSession, key: str = "acme") -> uuid.UUID:
    account_id = seed_id("account", key)
    subscription = (
        await session.execute(select(Subscription).where(Subscription.account_id == account_id))
    ).scalar_one()
    return subscription.id


class TestDirectBypassIsRefused:
    """§13: call the tool directly, bypassing the graph, and assert rejection."""

    async def test_mutation_without_any_approval_is_refused(self, session: AsyncSession) -> None:
        subscription_id = await _subscription_id(session)

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert not result.ok
        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_REQUIRED

    async def test_the_state_was_not_changed(self, session: AsyncSession) -> None:
        """A refusal that still mutated would be worse than no check at all."""
        subscription_id = await _subscription_id(session)
        before = (await session.get(Subscription, subscription_id)).plan_id  # type: ignore[union-attr]

        await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        after = (await session.get(Subscription, subscription_id)).plan_id  # type: ignore[union-attr]
        assert after == before


class TestApprovalScoping:
    async def test_a_granted_approval_permits_the_mutation(self, session: AsyncSession) -> None:
        subscription_id = await _subscription_id(session)
        await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
        )

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert result.ok, result.error
        assert result.data is not None
        assert result.data.new_plan_code == "enterprise"

    async def test_pending_is_not_approved(self, session: AsyncSession) -> None:
        """Exact status match: 'not rejected' must never read as authorised."""
        subscription_id = await _subscription_id(session)
        await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
            status=ApprovalStatus.PENDING,
        )

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert not result.ok
        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_NOT_GRANTED

    async def test_rejected_is_not_approved(self, session: AsyncSession) -> None:
        subscription_id = await _subscription_id(session)
        await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
            status=ApprovalStatus.REJECTED,
        )

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_NOT_GRANTED

    async def test_approval_for_another_entity_does_not_authorise_this_one(
        self, session: AsyncSession
    ) -> None:
        """Approval to upgrade Acme must not authorise upgrading Globex."""
        acme_subscription = await _subscription_id(session, "acme")
        globex_subscription = await _subscription_id(session, "globex")
        await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(acme_subscription),
        )

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(
                subscription_id=globex_subscription, target_plan_code="enterprise"
            ),
            handlers.update_subscription,
            approval_entity=("subscription", str(globex_subscription)),
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_REQUIRED

    async def test_approval_from_another_execution_does_not_carry_over(
        self, session: AsyncSession
    ) -> None:
        """Otherwise one human decision authorises every later workflow."""
        subscription_id = await _subscription_id(session)
        other = Approval(
            id=uuid.uuid4(),
            execution_id=uuid.uuid4(),  # a different run
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
            status=ApprovalStatus.APPROVED,
            reason="Granted for a different execution.",
            evidence={},
        )
        session.add(other)
        await session.flush()

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_REQUIRED

    async def test_an_approval_is_spent_after_one_use(self, session: AsyncSession) -> None:
        """A retry loop must not replay one decision into many mutations."""
        subscription_id = await _subscription_id(session)
        await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
        )
        arguments = UpdateSubscriptionInput(
            subscription_id=subscription_id, target_plan_code="enterprise"
        )

        first = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            arguments,
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )
        second = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            arguments,
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert first.ok
        assert not second.ok
        assert second.error is not None
        assert second.error.code == ToolErrorCode.APPROVAL_ALREADY_CONSUMED


class TestPermissionEnforcement:
    async def test_a_read_role_cannot_mutate_even_with_an_approval(
        self, session: AsyncSession
    ) -> None:
        """Permission and approval are independent gates."""
        subscription_id = await _subscription_id(session)
        await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
        )

        result = await execute_tool(
            _context(session, role=Role.RESEARCH),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.PERMISSION_DENIED

    async def test_a_read_tool_needs_no_approval(self, session: AsyncSession) -> None:
        result = await execute_tool(
            _context(session, role=Role.RESEARCH),
            ToolName.GET_SUBSCRIPTION,
            AccountInput(account_id=seed_id("account", "acme")),
            handlers.get_subscription,
        )

        assert result.ok, result.error

    async def test_mutation_without_an_execution_id_is_refused(self, session: AsyncSession) -> None:
        """There is nothing to scope an approval to."""
        account_id = seed_id("account", "acme")
        context = ToolContext(session=session, role=Role.EXECUTION, execution_id=None)

        result = await execute_tool(
            context,
            ToolName.UPDATE_CRM,
            UpdateCrmInput(account_id=account_id, plan_code="enterprise"),
            handlers.update_crm,
            approval_entity=("account", str(account_id)),
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_REQUIRED

    async def test_mutation_without_a_target_entity_is_refused(self, session: AsyncSession) -> None:
        """An unscoped approval would authorise every entity."""
        account_id = seed_id("account", "acme")

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_CRM,
            UpdateCrmInput(account_id=account_id, plan_code="enterprise"),
            handlers.update_crm,
            approval_entity=None,
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.INVALID_INPUT


class TestAuditTrail:
    async def test_every_call_writes_a_tool_call_row(self, session: AsyncSession) -> None:
        await execute_tool(
            _context(session, role=Role.RESEARCH),
            ToolName.GET_SUBSCRIPTION,
            AccountInput(account_id=seed_id("account", "acme")),
            handlers.get_subscription,
        )

        rows = list(
            (
                await session.execute(select(ToolCall).where(ToolCall.execution_id == EXECUTION_ID))
            ).scalars()
        )

        assert len(rows) == 1
        assert rows[0].tool_name == ToolName.GET_SUBSCRIPTION
        assert rows[0].succeeded is True
        assert rows[0].duration_ms is not None

    async def test_failures_are_recorded_too(self, session: AsyncSession) -> None:
        """A trace that only records successes cannot answer 'what did it try?'."""
        subscription_id = await _subscription_id(session)

        await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        row = (
            await session.execute(
                select(ToolCall).where(ToolCall.tool_name == ToolName.UPDATE_SUBSCRIPTION)
            )
        ).scalar_one()

        assert row.succeeded is False
        assert row.error_code == ToolErrorCode.APPROVAL_REQUIRED

    async def test_every_call_writes_an_audit_event(self, session: AsyncSession) -> None:
        await execute_tool(
            _context(session, role=Role.RESEARCH),
            ToolName.GET_SUBSCRIPTION,
            AccountInput(account_id=seed_id("account", "acme")),
            handlers.get_subscription,
        )

        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.execution_id == EXECUTION_ID)
                )
            ).scalars()
        )

        assert events
        assert events[0].actor_type == "agent"
        assert events[0].entity_id == ToolName.GET_SUBSCRIPTION

    async def test_a_successful_mutation_links_its_approval(self, session: AsyncSession) -> None:
        subscription_id = await _subscription_id(session)
        approval = await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
        )

        await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        row = (
            await session.execute(
                select(ToolCall).where(ToolCall.tool_name == ToolName.UPDATE_SUBSCRIPTION)
            )
        ).scalar_one()

        assert row.approval_id == approval.id


class TestStructuredFailures:
    async def test_a_missing_record_is_a_typed_code_not_an_exception(
        self, session: AsyncSession
    ) -> None:
        result = await execute_tool(
            _context(session, role=Role.RESEARCH),
            ToolName.GET_SUBSCRIPTION,
            AccountInput(account_id=uuid.uuid4()),
            handlers.get_subscription,
        )

        assert not result.ok
        assert result.error is not None
        assert result.error.code == ToolErrorCode.NOT_FOUND
        assert not result.error.retryable

    async def test_a_handler_crash_becomes_internal_error(self, session: AsyncSession) -> None:
        """No traceback reaches an agent's context."""

        async def exploding_handler(*_: object) -> None:
            raise RuntimeError("psycopg: password=hunter2 host=internal-db")

        result = await execute_tool(
            _context(session, role=Role.RESEARCH),
            ToolName.GET_CUSTOMER,
            AccountInput(account_id=uuid.uuid4()),
            exploding_handler,  # type: ignore[arg-type]
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.INTERNAL_ERROR
        assert "hunter2" not in result.error.message
        assert "RuntimeError" in result.error.message


class TestExpiredApprovalShape:
    async def test_expired_status_is_denied(self, session: AsyncSession) -> None:
        """A status added later must be denied by default, not authorised."""
        subscription_id = await _subscription_id(session)
        approval = await _grant(
            session,
            action="subscription_upgrade",
            entity_type="subscription",
            entity_id=str(subscription_id),
            status=ApprovalStatus.EXPIRED,
        )
        approval.requested_at = NOW - timedelta(days=7)

        result = await execute_tool(
            _context(session),
            ToolName.UPDATE_SUBSCRIPTION,
            UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code="enterprise"),
            handlers.update_subscription,
            approval_entity=("subscription", str(subscription_id)),
        )

        assert result.error is not None
        assert result.error.code == ToolErrorCode.APPROVAL_NOT_GRANTED
