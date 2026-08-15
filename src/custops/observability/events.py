"""The audit / structured-event catalogue.

BUILD_SPEC §16 fixes the vocabulary of workflow events. Defining it here — once,
as a closed enum — means the ``audit_events.event_type`` column has a meaning
that can be asserted in tests rather than a free-text string that drifts.

Stored as a plain ``VARCHAR`` rather than a PostgreSQL ``ENUM`` type: extending
a database enum requires a migration and locks, and later phases will add event
types. The constraint lives in the application layer where it is cheap to
evolve.

No event in this catalogue is emitted in Phase 1 — nothing creates workflows
yet. Phase 12 owns the write path.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """Structured workflow event names (BUILD_SPEC §16)."""

    REQUEST_RECEIVED = "request_received"
    WORKFLOW_CLASSIFIED = "workflow_classified"
    PLAN_CREATED = "plan_created"
    RETRIEVAL_STARTED = "retrieval_started"
    RETRIEVAL_COMPLETED = "retrieval_completed"
    TOOL_SELECTED = "tool_selected"
    TOOL_CALLED = "tool_called"
    TOOL_COMPLETED = "tool_completed"
    A2A_REQUEST_SENT = "a2a_request_sent"
    A2A_RESPONSE_RECEIVED = "a2a_response_received"
    DECISION_MADE = "decision_made"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RECEIVED = "approval_received"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_COMPLETED = "validation_completed"
    RETRY = "retry"
    REPLAN = "replan"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"


class ActorType(StrEnum):
    """Who caused an audited event.

    Distinguishing a human decision from an agent action from a system action is
    what makes the approval trail meaningful (BUILD_SPEC §13).
    """

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
