"""Merging three record types into one ordered trace.

Ordering is the part most likely to be subtly wrong, because it only misbehaves
under conditions a happy-path test never creates — specifically, several events
written inside one transaction.

``audit_events.occurred_at`` defaults to PostgreSQL's ``now()``, which is
*transaction start* time. Every row written in one transaction therefore shares
a timestamp. With the three write sites that existed before Phase 12 this was
invisible; a single tool call now writes three events in one savepoint, so a
sort on the timestamp alone could render the completion before the call.

These tests run without a database because the assembler is pure — which is the
reason it was written that way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custops.observability.events import WORKFLOW_EVENT_NAMES, EventType
from custops.observability.trace import (
    EntryKind,
    build_timeline,
    event_coverage,
    execution_ids_in,
)

T0 = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


@dataclass
class FakeStep:
    sequence: int
    node: str
    started_at: datetime
    duration_ms: int | None = 5
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeToolCall:
    tool_name: str
    started_at: datetime
    succeeded: bool = True
    error_code: str | None = None
    duration_ms: int | None = 3


@dataclass
class FakeEvent:
    id: int
    event_type: str
    occurred_at: datetime
    actor_type: str = "agent"
    actor_id: str | None = "execution"
    entity_type: str | None = None
    entity_id: str | None = None
    execution_id: Any = None
    payload: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------- ordering


def test_entries_from_all_three_sources_appear() -> None:
    timeline = build_timeline(
        steps=[FakeStep(0, "supervisor", T0)],
        tool_calls=[FakeToolCall("get_subscription", T0 + timedelta(seconds=1))],
        events=[FakeEvent(1, "request_received", T0 + timedelta(seconds=2))],
    )

    assert [entry.kind for entry in timeline] == [
        EntryKind.STEP,
        EntryKind.TOOL_CALL,
        EntryKind.EVENT,
    ]


def test_entries_are_ordered_by_time() -> None:
    timeline = build_timeline(
        steps=[],
        tool_calls=[],
        events=[
            FakeEvent(3, "workflow_completed", T0 + timedelta(seconds=9)),
            FakeEvent(1, "request_received", T0),
            FakeEvent(2, "decision_made", T0 + timedelta(seconds=4)),
        ],
    )

    assert [entry.label for entry in timeline] == [
        "request_received",
        "decision_made",
        "workflow_completed",
    ]


def test_events_sharing_a_timestamp_fall_back_to_insertion_order() -> None:
    """The defect this ordering rule exists for.

    All three rows carry the same ``occurred_at`` because they were written in
    one transaction. Only the monotonic id distinguishes them, and without it a
    tool's completion can sort before the call that produced it.
    """
    timeline = build_timeline(
        steps=[],
        tool_calls=[],
        events=[
            FakeEvent(12, "tool_completed", T0),
            FakeEvent(10, "tool_selected", T0),
            FakeEvent(11, "tool_called", T0),
        ],
    )

    assert [entry.label for entry in timeline] == [
        "tool_selected",
        "tool_called",
        "tool_completed",
    ]


def test_ordering_is_stable_across_repeated_assembly() -> None:
    """A trace that appears to change between two reads of the same execution
    is worse than a thin one."""
    events = [FakeEvent(index, f"event_{index}", T0) for index in range(6)]

    first = [entry.label for entry in build_timeline(steps=[], tool_calls=[], events=events)]
    second = [
        entry.label
        for entry in build_timeline(steps=[], tool_calls=[], events=list(reversed(events)))
    ]

    assert first == second


def test_steps_at_the_same_instant_keep_graph_sequence() -> None:
    timeline = build_timeline(
        steps=[FakeStep(2, "decide", T0), FakeStep(0, "supervisor", T0), FakeStep(1, "plan", T0)],
        tool_calls=[],
        events=[],
    )

    assert [entry.label for entry in timeline] == ["supervisor", "plan", "decide"]


def test_an_empty_execution_yields_an_empty_timeline() -> None:
    assert build_timeline(steps=[], tool_calls=[], events=[]) == []


# ------------------------------------------------------------------ redaction


def test_event_payloads_are_redacted_on_the_way_out() -> None:
    """The endpoint is the boundary that actually discloses.

    The recorder redacts on the way in, but rows may predate it, so the read
    path redacts too.
    """
    timeline = build_timeline(
        steps=[],
        tool_calls=[],
        events=[FakeEvent(1, "decision_made", T0, payload={"reasoning": "LEAKED", "ok": True})],
    )

    assert "LEAKED" not in str(timeline[0].detail)
    assert timeline[0].detail["payload"]["ok"] is True


def test_step_output_is_redacted_too() -> None:
    """Graph state reaches the endpoint through step output, so it is not
    exempt from the same rules."""
    timeline = build_timeline(
        steps=[FakeStep(0, "decide", T0, output={"thought": "LEAKED", "status": "executing"})],
        tool_calls=[],
        events=[],
    )

    assert "LEAKED" not in str(timeline[0].detail)


def test_secrets_in_step_output_are_masked() -> None:
    timeline = build_timeline(
        steps=[FakeStep(0, "execute", T0, output={"portal_password": "s3cret"})],
        tool_calls=[],
        events=[],
    )

    assert "s3cret" not in str(timeline[0].detail)


# ------------------------------------------------------------------- coverage


def test_coverage_reports_what_an_execution_emitted() -> None:
    coverage = event_coverage(
        [FakeEvent(1, str(EventType.REQUEST_RECEIVED), T0)], WORKFLOW_EVENT_NAMES
    )

    assert str(EventType.REQUEST_RECEIVED) in coverage.emitted
    assert str(EventType.VALIDATION_COMPLETED) in coverage.missing


def test_coverage_of_a_complete_run_has_nothing_missing() -> None:
    events = [FakeEvent(index, name, T0) for index, name in enumerate(sorted(WORKFLOW_EVENT_NAMES))]

    coverage = event_coverage(events, WORKFLOW_EVENT_NAMES)

    assert coverage.missing == frozenset()


def test_a_workflow_that_never_validated_shows_it_as_missing() -> None:
    """The point of the coverage view: an absent verdict is easier to see as a
    missing name than by reading a timeline top to bottom."""
    emitted = WORKFLOW_EVENT_NAMES - {str(EventType.VALIDATION_COMPLETED)}
    events = [FakeEvent(index, name, T0) for index, name in enumerate(sorted(emitted))]

    coverage = event_coverage(events, WORKFLOW_EVENT_NAMES)

    assert coverage.missing == {str(EventType.VALIDATION_COMPLETED)}


# ---------------------------------------------------------------- correlation


def test_execution_ids_are_collected_for_correlation_checks() -> None:
    import uuid

    execution_id = uuid.uuid4()
    events = [
        FakeEvent(1, "request_received", T0, execution_id=execution_id),
        FakeEvent(2, "decision_made", T0, execution_id=execution_id),
    ]

    assert execution_ids_in(events) == {execution_id}


def test_events_without_an_execution_id_are_not_counted() -> None:
    """An administrative action belongs to no workflow, and must not appear as
    a second execution in a correlation check."""
    assert execution_ids_in([FakeEvent(1, "approval_received", T0, execution_id=None)]) == set()
