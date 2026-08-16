"""Assembling one workflow's records into a single ordered timeline (§16).

Deliberately **pure**: it takes lists of already-loaded rows and returns
entries. No session, no query, no I/O. That is what lets the ordering rules
below be tested without PostgreSQL — and ordering is the part most likely to be
subtly wrong, because it only misbehaves under conditions a happy-path test
never creates.

**The ordering rule, and why it is not just ``occurred_at``.**

``audit_events.occurred_at`` defaults to PostgreSQL's ``now()``, which is
*transaction start time*, not wall clock. Every row written inside one
transaction therefore carries an identical timestamp. With the three write sites
that existed before Phase 12 this was invisible. Phase 12 takes a single tool
call from one event to three — ``tool_selected``, ``tool_called``,
``tool_completed``, all in the same savepoint — at which point sorting on the
timestamp alone can render the completion before the call.

``audit_events.id`` is a monotonic ``BigInteger Identity``, so it breaks the tie
in exact insertion order. Sorting by ``(occurred_at, id)`` keeps wall-clock
order across transactions and insertion order within one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from custops.observability.redaction import redact


class EntryKind(StrEnum):
    """Which record a timeline entry came from.

    Kept distinct rather than flattened because the three sources are written by
    different layers, and knowing *which* layer recorded something is half the
    value of the trace. A graph step and an audit event that disagree is a
    finding; merged into one undifferentiated list it would be invisible.
    """

    STEP = "step"
    TOOL_CALL = "tool_call"
    EVENT = "event"


class _StepRow(Protocol):
    sequence: int
    node: str
    started_at: datetime
    duration_ms: int | None
    output: dict[str, Any]


class _ToolCallRow(Protocol):
    tool_name: str
    succeeded: bool
    error_code: str | None
    started_at: datetime
    duration_ms: int | None


class _EventRow(Protocol):
    id: int
    event_type: str
    actor_type: str
    actor_id: str | None
    entity_type: str | None
    entity_id: str | None
    occurred_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One thing that happened, from whichever layer recorded it."""

    kind: EntryKind
    at: datetime
    label: str
    # Insertion order within the same instant. Only audit events carry a
    # monotonic id; steps and tool calls fall back to their own ordering keys.
    tiebreak: int
    detail: dict[str, Any] = field(default_factory=dict)


def build_timeline(
    *,
    steps: list[Any],
    tool_calls: list[Any],
    events: list[Any],
) -> list[TimelineEntry]:
    """Merge the three record types into one ordered timeline.

    Payload detail is passed through :func:`redact` again on the way out. The
    recorder already redacted on the way in, so this is belt and braces — but
    the rows may predate the recorder, and this function feeds an HTTP endpoint.
    Redacting at the boundary that discloses is worth the duplicated work.
    """
    entries: list[TimelineEntry] = []

    for step in steps:
        entries.append(
            TimelineEntry(
                kind=EntryKind.STEP,
                at=step.started_at,
                label=step.node,
                tiebreak=step.sequence,
                detail=redact(
                    {
                        "sequence": step.sequence,
                        "duration_ms": step.duration_ms,
                        "output": step.output,
                    }
                ),
            )
        )

    for index, call in enumerate(tool_calls):
        entries.append(
            TimelineEntry(
                kind=EntryKind.TOOL_CALL,
                at=call.started_at,
                label=call.tool_name,
                tiebreak=index,
                detail=redact(
                    {
                        "succeeded": call.succeeded,
                        "error_code": call.error_code,
                        "duration_ms": call.duration_ms,
                    }
                ),
            )
        )

    for event in events:
        entries.append(
            TimelineEntry(
                kind=EntryKind.EVENT,
                at=event.occurred_at,
                label=event.event_type,
                # The monotonic identity column: exact insertion order for rows
                # that share a transaction timestamp.
                tiebreak=int(event.id or 0),
                detail=redact(
                    {
                        "actor_type": event.actor_type,
                        "actor_id": event.actor_id,
                        "entity_type": event.entity_type,
                        "entity_id": event.entity_id,
                        "payload": event.payload,
                    }
                ),
            )
        )

    # Sort on time first, then insertion order, then kind so the result is
    # totally ordered — an unstable trace is one that appears to change between
    # two reads of the same execution.
    entries.sort(key=lambda entry: (entry.at, entry.tiebreak, str(entry.kind)))
    return entries


@dataclass(frozen=True, slots=True)
class EventCoverage:
    """Which §16 events a given execution actually produced.

    Useful in its own right: a workflow that completed without ever emitting
    ``validation_completed`` did not validate, and that is far easier to see as
    a missing name than by reading a timeline.
    """

    emitted: frozenset[str]
    missing: frozenset[str]


def event_coverage(events: list[Any], expected: frozenset[str]) -> EventCoverage:
    seen = frozenset(str(event.event_type) for event in events)
    return EventCoverage(emitted=seen, missing=expected - seen)


def execution_ids_in(events: list[Any]) -> set[uuid.UUID]:
    """Distinct execution ids present, for asserting correlation is intact."""
    return {event.execution_id for event in events if event.execution_id is not None}
