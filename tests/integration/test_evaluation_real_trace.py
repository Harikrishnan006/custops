"""The adapter against a workflow that actually ran (§15).

Every other adapter test uses synthetic CustOps rows. Those prove the mapping
logic, and they are what the CI gate runs on — but they cannot prove that the
rows a *real* execution writes carry what the adapter reads. A field renamed in
Phase 12, or a node that stopped recording its decisions, would leave the
synthetic tests green and the real adapter blind.

So this runs a genuine Subscription Upgrade through the orchestrator, reads its
trace back out of PostgreSQL, and puts it through the same adapter and the same
AgentForge scoring the gate uses.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from agent_forge.agent_eval import score_single_trace
from agent_forge.models import StepType
from sqlalchemy import select

from custops.db.engine import Database
from custops.domain.models.approval import ToolCall
from custops.domain.models.audit import AuditEvent
from custops.domain.models.workflow import WorkflowExecution, WorkflowStep
from custops.domain.seed import clear_seed_data, seed_all
from custops.evaluation.adapter import ExecutionRecord, to_agent_trace
from custops.evaluation.runner import load_tasks
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)


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


async def _load_record(database: Database, execution_id: uuid.UUID) -> ExecutionRecord:
    """Read one execution back exactly as the inspection endpoint does."""
    async with database.session_factory() as session:
        execution = await session.get(WorkflowExecution, execution_id)
        assert execution is not None

        steps = list(
            (
                await session.execute(
                    select(WorkflowStep)
                    .where(WorkflowStep.execution_id == execution_id)
                    .order_by(WorkflowStep.sequence)
                )
            ).scalars()
        )
        tool_calls = list(
            (
                await session.execute(
                    select(ToolCall)
                    .where(ToolCall.execution_id == execution_id)
                    .order_by(ToolCall.started_at)
                )
            ).scalars()
        )
        events = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.execution_id == execution_id)
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            ).scalars()
        )

    return ExecutionRecord(
        execution=execution, steps=steps, tool_calls=tool_calls, events=events
    )


class TestTheAdapterOnARealExecution:
    async def test_a_real_run_converts_to_a_scorable_trace(
        self, seeded: Database, live_client: object
    ) -> None:
        """End to end: run the workflow, read the trace, score it.

        This is the test that would catch a Phase 12 field the adapter depends
        on being renamed or dropped.
        """
        from httpx import AsyncClient

        assert isinstance(live_client, AsyncClient)

        started = await live_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        assert started.status_code in (200, 201, 202), started.text
        execution_id = uuid.UUID(started.json()["execution_id"])

        record = await _load_record(seeded, execution_id)
        trace = to_agent_trace(record, task_id="upgrade-happy-path")

        assert trace.task_id == "upgrade-happy-path"
        assert trace.task
        assert trace.steps, "a real execution produced no trace steps"
        # Every step must carry a type AgentForge understands; an unmapped
        # node silently producing nothing is the failure mode here.
        assert all(isinstance(step.type, StepType) for step in trace.steps)

    async def test_real_tool_calls_appear_as_call_and_result_pairs(
        self, seeded: Database, live_client: object
    ) -> None:
        from httpx import AsyncClient

        assert isinstance(live_client, AsyncClient)

        started = await live_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        execution_id = uuid.UUID(started.json()["execution_id"])

        record = await _load_record(seeded, execution_id)
        trace = to_agent_trace(record, task_id="upgrade-happy-path")

        calls = [s for s in trace.steps if s.type is StepType.TOOL_CALL]
        results = [s for s in trace.steps if s.type is StepType.TOOL_RESULT]

        assert calls, "a real run recorded no tool calls"
        assert len(calls) == len(results)
        assert all(step.tool_name for step in calls)
        # The sequence AgentForge compares against GoldenTask.expected_tools.
        assert trace.tool_sequence == [step.tool_name for step in calls]

    async def test_a_real_trace_scores_through_agentforge(
        self, seeded: Database, live_client: object
    ) -> None:
        """The whole point: a genuine execution reaching the same scoring the
        gate applies to the synthetic set."""
        from httpx import AsyncClient

        assert isinstance(live_client, AsyncClient)

        started = await live_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        execution_id = uuid.UUID(started.json()["execution_id"])

        record = await _load_record(seeded, execution_id)
        trace = to_agent_trace(record, task_id="upgrade-happy-path")
        task = next(t for t in load_tasks() if t.task_id == "upgrade-happy-path")

        score = score_single_trace(trace, task, use_judge=False)

        assert score.task_id == "upgrade-happy-path"
        # Hallucination is checked against the live permission matrix, so a real
        # run must never trip it — every tool it called exists by construction.
        assert score.tool_hallucination is False

    async def test_a_real_trace_carries_no_chain_of_thought(
        self, seeded: Database, live_client: object
    ) -> None:
        """Rule 18 across the whole pipeline, on real data rather than fixtures."""
        from httpx import AsyncClient

        assert isinstance(live_client, AsyncClient)

        started = await live_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        execution_id = uuid.UUID(started.json()["execution_id"])

        record = await _load_record(seeded, execution_id)
        rendered = str(to_agent_trace(record, task_id="upgrade-happy-path")).lower()

        for forbidden in ("chain_of_thought", "scratchpad", "inner_monologue", "raw_completion"):
            assert forbidden not in rendered
