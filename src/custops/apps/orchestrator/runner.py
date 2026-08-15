"""Running a workflow end to end (BUILD_SPEC Phase 6).

Assembles the graph with real nodes and a PostgreSQL checkpointer, runs it, and
records what happened where a human can read it.

**Runs inline, within the caller's request.** No queue, no worker — decision D2
cut Celery, and nothing here justifies bringing it back. That is acceptable
precisely because the graph does not block on humans: when it reaches the
approval gate it *interrupts* and `ainvoke` returns, so the longest a request
waits is the automated portion of one workflow. A run that pauses is resumed by
a later call, not by a held connection.

**Streaming, not just the final state.** ``astream`` yields each node's update
as it happens, which is what lets a step be recorded per visit — including the
second and third visit of a retry loop, which a final-state snapshot would hide.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from custops.agents.budgets import BudgetPolicy
from custops.agents.nodes import NodeDependencies, build_nodes
from custops.agents.state import WorkflowState, WorkflowStatus, initial_state
from custops.apps.orchestrator.checkpointer import open_checkpointer
from custops.apps.orchestrator.graph import compile_graph
from custops.config import Settings
from custops.db.engine import Database
from custops.domain.models.workflow import WorkflowExecution, WorkflowStep
from custops.observability.context import bind_context
from custops.observability.events import ActorType, EventType
from custops.observability.logging import get_logger
from custops.providers.chat import ChatProvider
from custops.providers.registry import get_embedding_provider

logger = get_logger(__name__)

# Keys that are graph plumbing rather than workflow state, and must not be
# persisted as if they were.
_INTERNAL_KEYS = frozenset({"__interrupt__"})


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What a run produced, and whether it is finished.

    ``interrupt_payload`` is what a human must answer. Its presence is the
    single signal that this run is paused rather than complete — derived from
    the graph itself, not inferred from a status string a node happened to set.
    """

    execution_id: uuid.UUID
    status: str
    state: WorkflowState
    interrupt_payload: dict[str, Any] | None
    steps: list[str]

    @property
    def paused(self) -> bool:
        return self.interrupt_payload is not None


class WorkflowRunner:
    """Starts and resumes Subscription Upgrade runs."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        chat: ChatProvider,
        budget_policy: BudgetPolicy | None = None,
        in_memory_checkpointer: bool = False,
    ) -> None:
        self._settings = settings
        self._database = database
        self._chat = chat
        self._budget_policy = budget_policy
        self._in_memory = in_memory_checkpointer

    async def start(self, *, raw_request: str, request_id: str | None = None) -> RunOutcome:
        """Begin a new run, returning when it completes or pauses."""
        execution_id = uuid.uuid4()
        started_at = datetime.now(UTC)

        state = initial_state(
            execution_id=execution_id,
            request_id=request_id or str(execution_id),
            raw_request=raw_request,
            started_at=started_at,
        )

        await self._record_started(state)
        return await self._drive(execution_id, state, started_at)

    async def resume(self, *, execution_id: uuid.UUID, decision: dict[str, Any]) -> RunOutcome:
        """Resume a paused run with a human's decision.

        Phase 7 owns the endpoint that calls this; the runner supports it now
        because the approval gate is already interrupting and a pause with no
        way back is not a workflow.
        """
        return await self._drive(
            execution_id, Command(resume=decision), datetime.now(UTC), resuming=True
        )

    async def _drive(
        self,
        execution_id: uuid.UUID,
        payload: WorkflowState | Command[Any],
        started_at: datetime,
        *,
        resuming: bool = False,
    ) -> RunOutcome:
        """Stream the graph, recording each node visit."""
        embedder = get_embedding_provider(self._settings)
        deps = NodeDependencies(
            session_factory=self._database.session_factory,
            chat=self._chat,
            embedder=embedder,
        )
        nodes = build_nodes(deps)
        config: RunnableConfig = {"configurable": {"thread_id": str(execution_id)}}

        visited: list[str] = []
        merged: dict[str, Any] = {}
        interrupt_payload: dict[str, Any] | None = None

        # Every log line, tool call and audit event emitted below carries this
        # execution_id (§16).
        with bind_context(execution_id=str(execution_id)):
            logger.info(
                "workflow_started" if not resuming else "workflow_resumed",
                execution_id=str(execution_id),
            )

            async with open_checkpointer(self._settings, in_memory=self._in_memory) as checkpointer:
                app = compile_graph(
                    nodes, checkpointer=checkpointer, budget_policy=self._budget_policy
                )

                sequence = await self._next_sequence(execution_id)
                async for chunk in app.astream(payload, config=config, stream_mode="updates"):
                    for node, update in chunk.items():
                        if node in _INTERNAL_KEYS:
                            continue
                        elapsed = time.perf_counter()
                        visited.append(node)
                        if isinstance(update, dict):
                            merged.update(update)
                        await self._record_step(execution_id, sequence, node, update, elapsed)
                        sequence += 1

                snapshot = await app.aget_state(config)
                interrupt_payload = _interrupt_from(snapshot)
                final = dict(snapshot.values) if snapshot.values else merged

        state: WorkflowState = final  # type: ignore[assignment]
        status = _resolve_status(state, paused=interrupt_payload is not None)
        await self._record_finished(
            execution_id, state, status, paused=interrupt_payload is not None
        )

        logger.info(
            "workflow_finished",
            execution_id=str(execution_id),
            status=status,
            steps=len(visited),
        )
        return RunOutcome(
            execution_id=execution_id,
            status=status,
            state=state,
            interrupt_payload=interrupt_payload,
            steps=visited,
        )

    # -- persistence -------------------------------------------------------

    async def _record_started(self, state: WorkflowState) -> None:
        async with self._database.session_factory() as session:
            session.add(
                WorkflowExecution(
                    id=state["execution_id"],
                    request_id=state.get("request_id"),
                    raw_request=state["raw_request"],
                    workflow_type=str(state.get("workflow_type")),
                    status=WorkflowStatus.RECEIVED,
                    started_at=state["started_at"],
                    final_state={},
                )
            )
            await session.commit()

    async def _next_sequence(self, execution_id: uuid.UUID) -> int:
        """Continue numbering across a resume rather than restarting at zero."""
        from sqlalchemy import func, select

        async with self._database.session_factory() as session:
            highest = (
                await session.execute(
                    select(func.max(WorkflowStep.sequence)).where(
                        WorkflowStep.execution_id == execution_id
                    )
                )
            ).scalar()
            return int(highest) + 1 if highest is not None else 0

    async def _record_step(
        self,
        execution_id: uuid.UUID,
        sequence: int,
        node: str,
        update: Any,
        started: float,
    ) -> None:
        async with self._database.session_factory() as session:
            session.add(
                WorkflowStep(
                    execution_id=execution_id,
                    sequence=sequence,
                    node=node,
                    output=_serialisable(update),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            await session.commit()

    async def _record_finished(
        self,
        execution_id: uuid.UUID,
        state: WorkflowState,
        status: str,
        *,
        paused: bool,
    ) -> None:
        from custops.domain.models.audit import AuditEvent

        async with self._database.session_factory() as session:
            execution = await session.get(WorkflowExecution, execution_id)
            if execution is not None:
                execution.status = status
                execution.workflow_type = str(state.get("workflow_type"))
                execution.customer_ref = state.get("customer_ref")
                execution.account_id = state.get("account_id")
                execution.target_plan_code = state.get("target_plan_code")
                execution.retry_count = int(state.get("retry_count", 0))
                execution.replan_count = int(state.get("replan_count", 0))
                execution.escalation_reason = state.get("escalation_reason")
                execution.final_state = _summary(state)
                # A paused run is not finished. Stamping it would make an
                # awaiting-approval workflow indistinguishable from a completed
                # one in every report that filters on finished_at.
                execution.finished_at = None if paused else datetime.now(UTC)

            session.add(
                AuditEvent(
                    execution_id=execution_id,
                    event_type=(
                        EventType.WORKFLOW_COMPLETED
                        if status == WorkflowStatus.COMPLETED
                        else EventType.WORKFLOW_FAILED
                    ),
                    actor_type=ActorType.SYSTEM,
                    actor_id="orchestrator",
                    entity_type="workflow_execution",
                    entity_id=str(execution_id),
                    payload={"status": status, "paused": paused},
                )
            )
            await session.commit()


def _interrupt_from(snapshot: Any) -> dict[str, Any] | None:
    """Pull the pending interrupt payload out of a state snapshot."""
    interrupts = getattr(snapshot, "interrupts", None) or ()
    for item in interrupts:
        value = getattr(item, "value", None)
        if isinstance(value, dict):
            return value
    return None


def _resolve_status(state: WorkflowState, *, paused: bool) -> str:
    """The run's status, with pausing taking precedence.

    A node sets ``AWAITING_APPROVAL`` before it interrupts, but the graph is the
    authority on whether the run actually stopped there.
    """
    if paused:
        return WorkflowStatus.AWAITING_APPROVAL
    return str(state.get("status", WorkflowStatus.FAILED))


def _summary(state: WorkflowState) -> dict[str, Any]:
    """The structured record of a run.

    Decisions, evidence *citations*, validation results and errors — never the
    evidence text itself (it lives in the systems of record) and never
    chain-of-thought (Rule 18).
    """
    return {
        "workflow_type": str(state.get("workflow_type")),
        "decisions": state.get("decisions", []),
        "evidence_citations": [
            item.get("source_ref") for item in state.get("evidence", []) if item.get("source_ref")
        ],
        "validation_results": state.get("validation_results", []),
        "execution_results": state.get("execution_results", []),
        "errors": state.get("errors", []),
        "approval_status": state.get("approval_status"),
        "metadata": state.get("metadata", {}),
    }


def _serialisable(update: Any) -> dict[str, Any]:
    """Coerce a node update into something JSONB accepts."""
    if not isinstance(update, dict):
        return {"value": str(update)}
    return {key: _coerce(value) for key, value in update.items()}


def _coerce(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {key: _coerce(inner) for key, inner in value.items()}
    return value
