"""Transport schemas for the workflow API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StartWorkflowRequest(BaseModel):
    """A natural-language customer-operations request (§1)."""

    request: str = Field(min_length=1, max_length=4000)
    request_id: str | None = Field(default=None, max_length=64)


class DecisionOut(BaseModel):
    """A decision, structurally. No chain-of-thought (Rule 18)."""

    name: str
    outcome: str
    confidence: float
    rationale_summary: str
    evidence_refs: list[str]
    decided_at: str


class ValidationResultOut(BaseModel):
    check: str
    system: str
    verdict: str
    expected: str
    actual: str


class ApprovalPromptOut(BaseModel):
    """What a human must answer for a paused run (§13)."""

    approval_id: str
    action: str
    entity: str
    target_plan: str | None = None
    amount: str | None = None
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class WorkflowRunOut(BaseModel):
    """The outcome of starting or resuming a run."""

    execution_id: uuid.UUID
    status: str
    workflow_type: str
    customer_ref: str | None = None
    target_plan_code: str | None = None

    decisions: list[DecisionOut] = Field(default_factory=list)
    validation_results: list[ValidationResultOut] = Field(default_factory=list)
    evidence_citations: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    escalation_reason: str | None = None
    # Present only while the run is paused. Its presence is the signal, not a
    # status string a node happened to set.
    awaiting_approval: ApprovalPromptOut | None = None

    steps_visited: list[str] = Field(default_factory=list)


class StepOut(BaseModel):
    sequence: int
    node: str
    duration_ms: int | None
    started_at: datetime
    output: dict[str, Any]


class ToolCallOut(BaseModel):
    tool_name: str
    succeeded: bool | None
    error_code: str | None
    duration_ms: int | None
    started_at: datetime


class AuditEventOut(BaseModel):
    event_type: str
    actor_type: str
    actor_id: str | None
    entity_type: str | None
    entity_id: str | None
    occurred_at: datetime
    # Passed through the redaction boundary on the way out. The recorder already
    # redacted on the way in; doing it again here is deliberate, because this is
    # the boundary that actually discloses, and rows may predate the recorder.
    payload: dict[str, Any] = Field(default_factory=dict)


class TimelineEntryOut(BaseModel):
    """One thing that happened, from whichever layer recorded it (§16).

    The unified view. Steps, tool calls and audit events are written by
    different layers and only become a *trace* once merged and ordered — and
    ``kind`` is kept so a reader can still tell which layer said what.
    """

    kind: str
    at: datetime
    label: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EventCoverageOut(BaseModel):
    """Which §16 events this execution produced, and which it did not.

    A workflow that finished without ever emitting ``validation_completed`` did
    not validate. That is far easier to see as a missing name than by reading a
    timeline top to bottom.
    """

    emitted: list[str]
    missing: list[str]


class WorkflowTraceOut(BaseModel):
    """A full trace reconstructed from one execution_id (§16).

    Joins the graph's step record to the tool calls and audit events written
    under the same id — the three views a reviewer needs to answer what
    happened, what it touched, and what it decided.
    """

    execution_id: uuid.UUID
    request_id: str | None
    raw_request: str
    workflow_type: str
    status: str
    customer_ref: str | None
    target_plan_code: str | None
    retry_count: int
    replan_count: int
    escalation_reason: str | None
    started_at: datetime
    finished_at: datetime | None

    steps: list[StepOut]
    tool_calls: list[ToolCallOut]
    audit_events: list[AuditEventOut]
    # The merged, deterministically ordered view of all three record types.
    timeline: list[TimelineEntryOut] = Field(default_factory=list)
    event_coverage: EventCoverageOut | None = None
    final_state: dict[str, Any]
