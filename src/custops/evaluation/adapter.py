"""Turning a CustOps execution into the ``AgentTrace`` AgentForge scores.

This is the whole integration seam. AgentForge knows how to score a trace; it
cannot know what a LangGraph superstep, an MCP tool call or a cross-system
validation verdict is. The mapping lives here and nowhere else.

**The six step types, and how CustOps distinguishes them.**

``TOOL_CALL`` / ``TOOL_RESULT``
    One ``tool_calls`` row becomes both: the call carries the tool name and its
    arguments, the result carries what came back and whether it succeeded.

``REASONING``
    A graph node that decided something. The content is the node's
    ``rationale_summary`` or outcome — **never the deliberation that produced
    it**. Rule 18 applies here exactly as it does to the audit payload, and the
    trace is passed through the same redaction as everything else.

``FINAL_ANSWER``
    What the workflow told the customer, or the terminal state summary.

``REFUSAL`` vs ``ESCALATION``
    Both make ``AgentTrace.escalated`` true, so AgentForge scores guardrail
    tasks identically either way. The distinction is kept because it means
    different things to a reader and to the CustOps-specific evaluators:

    * **REFUSAL** — the platform *declined on policy grounds*. Eligibility
      blocked the upgrade, or a human rejected the approval. The system worked.
    * **ESCALATION** — the platform *could not finish*. A tool errored,
      validation found divergence, a budget ran out. Something needs a human
      because something went wrong.

    Collapsing them would make "we correctly refused a churned customer"
    indistinguishable from "the portal timed out", which is the difference
    between a healthy guardrail and an outage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from agent_forge.models import AgentTrace, StepType, TraceStep

from custops.agents.state import WorkflowStatus
from custops.observability.events import EventType
from custops.observability.redaction import redact

# Nodes whose visit represents a decision worth showing as a reasoning step.
# Deliberately a small set: a trace listing every graph visit would inflate
# step counts and make AgentForge's step-efficiency metric meaningless.
REASONING_NODES = frozenset({"supervisor", "planner", "plan", "research", "decide", "validate"})

# Nodes that produce the customer-facing answer.
ANSWER_NODES = frozenset({"notify", "complete"})

# Terminal statuses that are not a successful completion. Both can hide either
# a policy refusal or a genuine failure, which is why the audit trail — not the
# status — decides which of the two it was.
NON_SUCCESS_STATUSES = frozenset({WorkflowStatus.ESCALATED, WorkflowStatus.FAILED})


class _StepRow(Protocol):
    sequence: int
    node: str
    output: dict[str, Any]


class _ToolCallRow(Protocol):
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    succeeded: bool
    error_code: str | None
    duration_ms: int | None


class _EventRow(Protocol):
    event_type: str
    payload: dict[str, Any]


class _ExecutionRow(Protocol):
    id: uuid.UUID
    raw_request: str
    status: str
    escalation_reason: str | None
    retry_count: int
    replan_count: int
    final_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """Everything Phase 12 records about one execution.

    Passed as plain rows rather than a session, so the adapter is pure and can
    be tested — and audited — without a database.
    """

    execution: Any
    steps: list[Any]
    tool_calls: list[Any]
    events: list[Any]


def _terminal_kind(record: ExecutionRecord) -> StepType | None:
    """Decide whether the run refused, escalated, or did neither.

    Reads the audit trail rather than the status alone, because "escalated" in
    ``WorkflowExecution.status`` covers both a policy refusal and an outright
    failure. Phase 12's ``decision_made`` and ``approval_received`` events are
    what make them separable.
    """
    execution = record.execution

    if str(execution.status) not in NON_SUCCESS_STATUSES:
        return None

    for event in record.events:
        payload = event.payload or {}
        # A human said no. That is a refusal, not a malfunction.
        if (
            event.event_type == str(EventType.APPROVAL_RECEIVED)
            and payload.get("approved") is False
        ):
            return StepType.REFUSAL
        # Deterministic eligibility rules blocked it. Also a refusal.
        if (
            event.event_type == str(EventType.DECISION_MADE)
            and payload.get("outcome") == "blocked"
        ):
            return StepType.REFUSAL

    return StepType.ESCALATION


def _reasoning_content(node: str, output: dict[str, Any]) -> str:
    """A conclusion, never the reasoning that produced it (Rule 18).

    Reads only fields the domain models already bound and the spec already
    permits: a decision's outcome and its one-line ``rationale_summary``. Graph
    state is redacted on the way in regardless, so a field that ever came to
    hold deliberation would be dropped before reaching here.
    """
    safe = redact(output)
    parts: list[str] = []

    for decision in safe.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        name = decision.get("name")
        outcome = decision.get("outcome")
        summary = decision.get("rationale_summary")
        parts.append(f"{name}={outcome}" + (f" ({summary})" if summary else ""))

    if not parts:
        status = safe.get("status")
        parts.append(f"{node}: {status}" if status else node)

    return "; ".join(str(part) for part in parts)


def _final_answer(record: ExecutionRecord) -> str | None:
    """What the workflow concluded, in the customer's terms where one exists."""
    for step in reversed(record.steps):
        if step.node in ANSWER_NODES:
            safe = redact(step.output or {})
            notification = safe.get("notification")
            if isinstance(notification, dict):
                subject = notification.get("subject")
                body = notification.get("body")
                if subject or body:
                    return f"{subject or ''} {body or ''}".strip()

    execution = record.execution
    if execution.escalation_reason:
        return str(execution.escalation_reason)

    final_state = redact(execution.final_state or {})
    if final_state:
        return "; ".join(f"{key}={value}" for key, value in sorted(final_state.items()))
    return None


def to_agent_trace(
    record: ExecutionRecord,
    *,
    task_id: str,
    agent_version: str = "custops",
    cost_usd: float = 0.0,
) -> AgentTrace:
    """Build the ``AgentTrace`` AgentForge will score.

    Step order follows the graph's own ``sequence``, with each node's tool calls
    interleaved after it. Tool calls carry no sequence of their own, so they are
    attached in the order the tool layer recorded them — which Phase 12's
    ``(occurred_at, id)`` ordering guarantees is causal.
    """
    steps: list[TraceStep] = []
    index = 0

    for graph_step in sorted(record.steps, key=lambda step: step.sequence):
        if graph_step.node in REASONING_NODES:
            index += 1
            steps.append(
                TraceStep(
                    step=index,
                    type=StepType.REASONING,
                    content=_reasoning_content(graph_step.node, graph_step.output or {}),
                )
            )

    for call in record.tool_calls:
        index += 1
        steps.append(
            TraceStep(
                step=index,
                type=StepType.TOOL_CALL,
                tool_name=call.tool_name,
                tool_args=redact(call.arguments or {}),
            )
        )
        index += 1
        steps.append(
            TraceStep(
                step=index,
                type=StepType.TOOL_RESULT,
                tool_name=call.tool_name,
                tool_result=(
                    _summarise_result(call)
                    if call.succeeded
                    else f"ERROR: {call.error_code or 'failed'}"
                ),
            )
        )

    terminal = _terminal_kind(record)
    answer = _final_answer(record)

    index += 1
    if terminal is not None:
        # A refusal or escalation *is* the outcome. Emitting a FINAL_ANSWER
        # alongside it would make AgentForge see a run that both declined and
        # answered.
        steps.append(
            TraceStep(
                step=index,
                type=terminal,
                content=answer or str(record.execution.escalation_reason or terminal),
            )
        )
    else:
        steps.append(TraceStep(step=index, type=StepType.FINAL_ANSWER, content=answer or ""))

    return AgentTrace(
        task_id=task_id,
        task=record.execution.raw_request,
        steps=steps,
        final_answer=answer,
        cost_usd=cost_usd,
        latency_ms=_latency_ms(record),
        agent_version=agent_version,
    )


def _summarise_result(call: Any) -> str:
    """A bounded description of what a tool returned.

    Not the whole payload: AgentForge only needs enough to tell a result from an
    error, and a trace carrying every tool's full output would be a second copy
    of the database.
    """
    result = redact(call.result or {})
    if not isinstance(result, dict) or not result:
        return "ok"
    keys = sorted(str(key) for key in result)
    return "ok: " + ", ".join(keys[:8])


def _latency_ms(record: ExecutionRecord) -> int:
    """Total tool time. Deliberately not wall-clock.

    A run that paused at the approval gate for two days would otherwise report a
    172-million-millisecond latency, which says something about the human and
    nothing about the platform.
    """
    return sum(int(call.duration_ms or 0) for call in record.tool_calls)


def tool_sequence_of(record: ExecutionRecord) -> list[str]:
    """The tools actually called, in order — what AgentForge compares against
    ``GoldenTask.expected_tools``."""
    return [call.tool_name for call in record.tool_calls]
