"""Workflow endpoints (BUILD_SPEC Phase 6, §4).

Three operations: start a run, read a run's trace, list runs.

**What is deliberately absent:** an endpoint to approve or reject. A run that
pauses at the approval gate stays paused and says so, because recording a human
decision is Phase 7's job and inventing a thin version here would put the
approval record in two places. The runner already supports resuming; only the
route is missing.

The status code carries meaning: **201** for a run that finished, **202** for one
that paused awaiting a human. A client polling `/workflows/{id}` can tell the
difference without parsing a status string.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.apps.api.dependencies import get_database
from custops.apps.api.schemas.workflow import (
    ApprovalPromptOut,
    AuditEventOut,
    EventCoverageOut,
    StartWorkflowRequest,
    StepOut,
    TimelineEntryOut,
    ToolCallOut,
    WorkflowRunOut,
    WorkflowTraceOut,
)
from custops.apps.enterprise.router import get_session
from custops.apps.orchestrator.runner import RunOutcome, WorkflowRunner
from custops.config import Settings, get_settings
from custops.db.engine import Database
from custops.domain.models.approval import ToolCall
from custops.domain.models.audit import AuditEvent
from custops.domain.models.workflow import WorkflowExecution, WorkflowStep
from custops.observability.events import WORKFLOW_EVENT_NAMES
from custops.observability.redaction import redact
from custops.observability.trace import build_timeline, event_coverage
from custops.providers.chat import ChatProvider, DeterministicChatProvider

router = APIRouter(prefix="/workflows", tags=["workflows"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_chat_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChatProvider:
    """The chat provider for workflow runs.

    Overridden in tests. Only the deterministic stand-in is wired today: the
    real adapters need an API key, and returning a fabricated classification
    would be worse than an honest stand-in that is refused outside local/test.
    """
    return DeterministicChatProvider()


def get_runner(
    settings: Annotated[Settings, Depends(get_settings)],
    database: Annotated[Database, Depends(get_database)],
    chat: Annotated[ChatProvider, Depends(get_chat_provider)],
) -> WorkflowRunner:
    return WorkflowRunner(settings=settings, database=database, chat=chat)


@router.post(
    "",
    response_model=WorkflowRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a customer-operations workflow",
)
async def start_workflow(
    payload: StartWorkflowRequest,
    response: Response,
    runner: Annotated[WorkflowRunner, Depends(get_runner)],
) -> WorkflowRunOut:
    """Run a request end to end, returning when it completes or pauses.

    Synchronous by design: the graph interrupts rather than blocking on a human,
    so the request only ever waits for the automated portion (see runner.py).
    """
    outcome = await runner.start(raw_request=payload.request, request_id=payload.request_id)
    if outcome.paused:
        response.status_code = status.HTTP_202_ACCEPTED
    return _to_run_out(outcome)


@router.get(
    "/{execution_id}",
    response_model=WorkflowTraceOut,
    summary="Reconstruct a full execution trace",
)
async def get_workflow_trace(execution_id: uuid.UUID, session: SessionDep) -> WorkflowTraceOut:
    """Everything recorded under one execution_id (§16).

    Joins three independently written records — graph steps, tool calls and
    audit events — by the id they all carry. That they agree is itself
    informative; that they are written by different layers is what makes the
    trace worth trusting.
    """
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workflow execution {execution_id}.",
        )

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
                # `occurred_at` defaults to PostgreSQL now(), which is
                # *transaction* time — every row written in one transaction
                # shares it. `id` is monotonic, so it breaks the tie in exact
                # insertion order. Without it a tool's completion can sort
                # before the call that produced it.
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        ).scalars()
    )

    coverage = event_coverage(events, WORKFLOW_EVENT_NAMES)

    return WorkflowTraceOut(
        execution_id=execution.id,
        request_id=execution.request_id,
        raw_request=execution.raw_request,
        workflow_type=execution.workflow_type,
        status=execution.status,
        customer_ref=execution.customer_ref,
        target_plan_code=execution.target_plan_code,
        retry_count=execution.retry_count,
        replan_count=execution.replan_count,
        escalation_reason=execution.escalation_reason,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        steps=[
            StepOut(
                sequence=step.sequence,
                node=step.node,
                duration_ms=step.duration_ms,
                started_at=step.started_at,
                output=step.output,
            )
            for step in steps
        ],
        tool_calls=[
            ToolCallOut(
                tool_name=call.tool_name,
                succeeded=call.succeeded,
                error_code=call.error_code,
                duration_ms=call.duration_ms,
                started_at=call.started_at,
            )
            for call in tool_calls
        ],
        audit_events=[
            AuditEventOut(
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                occurred_at=event.occurred_at,
                payload=redact(event.payload),
            )
            for event in events
        ],
        timeline=[
            TimelineEntryOut(
                kind=str(entry.kind),
                at=entry.at,
                label=entry.label,
                detail=entry.detail,
            )
            for entry in build_timeline(steps=steps, tool_calls=tool_calls, events=events)
        ],
        event_coverage=EventCoverageOut(
            emitted=sorted(coverage.emitted),
            missing=sorted(coverage.missing),
        ),
        final_state=redact(execution.final_state),
    )


@router.get("", response_model=list[WorkflowTraceOut], summary="List recent runs")
async def list_workflows(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[WorkflowTraceOut]:
    """Recent runs, newest first, without their step detail.

    Steps, tool calls and audit events are omitted here — a list endpoint that
    loaded every trace in full would be a slow way to answer "what ran lately".
    """
    executions = list(
        (
            await session.execute(
                select(WorkflowExecution).order_by(WorkflowExecution.started_at.desc()).limit(limit)
            )
        ).scalars()
    )
    return [
        WorkflowTraceOut(
            execution_id=execution.id,
            request_id=execution.request_id,
            raw_request=execution.raw_request,
            workflow_type=execution.workflow_type,
            status=execution.status,
            customer_ref=execution.customer_ref,
            target_plan_code=execution.target_plan_code,
            retry_count=execution.retry_count,
            replan_count=execution.replan_count,
            escalation_reason=execution.escalation_reason,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            steps=[],
            tool_calls=[],
            audit_events=[],
            final_state=execution.final_state,
        )
        for execution in executions
    ]


def _to_run_out(outcome: RunOutcome) -> WorkflowRunOut:
    state: dict[str, Any] = dict(outcome.state)
    prompt = outcome.interrupt_payload

    return WorkflowRunOut(
        execution_id=outcome.execution_id,
        status=outcome.status,
        workflow_type=str(state.get("workflow_type", "unknown")),
        customer_ref=state.get("customer_ref"),
        target_plan_code=state.get("target_plan_code"),
        decisions=state.get("decisions", []),
        validation_results=state.get("validation_results", []),
        evidence_citations=[
            item.get("source_ref") for item in state.get("evidence", []) if item.get("source_ref")
        ],
        errors=state.get("errors", []),
        escalation_reason=state.get("escalation_reason"),
        awaiting_approval=(
            ApprovalPromptOut(
                approval_id=str(prompt.get("approval_id")),
                action=str(prompt.get("action")),
                entity=str(prompt.get("entity")),
                target_plan=prompt.get("target_plan"),
                amount=prompt.get("amount"),
                reasons=list(prompt.get("reasons") or []),
                evidence=[e for e in (prompt.get("evidence") or []) if e],
            )
            if prompt
            else None
        ),
        steps_visited=outcome.steps,
    )
