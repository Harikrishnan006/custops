"""The single recording path (§16).

Driven against a fake session rather than PostgreSQL: what matters here is the
row the recorder *builds* — its correlation, its actor, and the fact that the
payload passed through redaction. Whether PostgreSQL then stores it is the
integration suite's question.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from custops.observability.audit import record_event
from custops.observability.context import bind_context
from custops.observability.events import ActorType, EventType
from custops.observability.redaction import REDACTED


class FakeSession:
    """Records what was added and whether it was flushed.

    Deliberately not a mock: the recorder's contract is "adds a row and flushes
    within the caller's transaction", and asserting on a real object makes a
    stray ``commit()`` visible rather than silently absorbed.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushes = 0
        self.commits = 0

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:  # pragma: no cover - must never be called
        self.commits += 1


async def _record(**kwargs: Any) -> Any:
    session = FakeSession()
    await record_event(
        session,  # type: ignore[arg-type]
        kwargs.pop("event_type", EventType.DECISION_MADE),
        actor_type=kwargs.pop("actor_type", ActorType.AGENT),
        **kwargs,
    )
    return session


# ------------------------------------------------------------------- the row


async def test_it_adds_exactly_one_row_and_flushes() -> None:
    session = await _record()

    assert len(session.added) == 1
    assert session.flushes == 1


async def test_it_never_commits_the_caller_s_transaction() -> None:
    """An audit write must join the caller's transaction, not end it.

    Committing here would persist whatever half-finished work the caller had in
    flight — which for the tool layer would mean committing a mutation whose
    approval had not yet been verified.
    """
    session = await _record()

    assert session.commits == 0


async def test_flush_can_be_deferred_for_a_caller_already_inside_one() -> None:
    session = FakeSession()

    await record_event(
        session,  # type: ignore[arg-type]
        EventType.TOOL_CALLED,
        actor_type=ActorType.AGENT,
        flush=False,
    )

    assert len(session.added) == 1
    assert session.flushes == 0


async def test_the_event_type_and_actor_are_stored_as_plain_strings() -> None:
    """The column is VARCHAR; storing an enum member would round-trip oddly."""
    session = await _record(event_type=EventType.RETRY, actor_type=ActorType.SYSTEM)
    event = session.added[0]

    assert event.event_type == "retry"
    assert event.actor_type == "system"
    assert isinstance(event.event_type, str)


# --------------------------------------------------------------- correlation


async def test_an_explicit_execution_id_is_used() -> None:
    execution_id = uuid.uuid4()

    session = await _record(execution_id=execution_id)

    assert session.added[0].execution_id == execution_id


async def test_the_ambient_execution_id_is_picked_up_when_none_is_passed() -> None:
    """The seam that stops a correlation id going missing.

    Nodes deep inside a graph should not have to thread an id through every
    call to stay traceable.
    """
    execution_id = uuid.uuid4()

    with bind_context(execution_id=str(execution_id)):
        session = await _record()

    assert session.added[0].execution_id == execution_id


async def test_an_explicit_id_beats_the_ambient_one() -> None:
    """The approval endpoint records an event *about* an execution it is not
    running inside, so the ambient context is the wrong answer there."""
    ambient, explicit = uuid.uuid4(), uuid.uuid4()

    with bind_context(execution_id=str(ambient)):
        session = await _record(execution_id=explicit)

    assert session.added[0].execution_id == explicit


async def test_no_execution_id_anywhere_is_allowed() -> None:
    """Not every audited action belongs to a workflow."""
    session = await _record()

    assert session.added[0].execution_id is None


async def test_an_unparseable_ambient_id_does_not_fail_the_write() -> None:
    """Losing the record that explains what happened, because a correlation id
    was malformed, would be the worst possible trade."""
    with bind_context(execution_id="not-a-uuid"):
        session = await _record()

    assert len(session.added) == 1
    assert session.added[0].execution_id is None


async def test_the_request_id_is_carried() -> None:
    with bind_context(execution_id=str(uuid.uuid4()), request_id="req-42"):
        session = await _record()

    assert session.added[0].request_id == "req-42"


# ----------------------------------------------------------------- redaction


async def test_the_payload_is_redacted_on_the_way_in() -> None:
    """No call site can opt out, which is the whole reason the recorder exists."""
    session = await _record(payload={"reasoning": "LEAKED", "outcome": "eligible"})
    payload = session.added[0].payload

    assert "LEAKED" not in str(payload)
    assert payload["outcome"] == "eligible"


async def test_secrets_in_a_payload_are_masked_on_the_way_in() -> None:
    session = await _record(payload={"arguments": {"password": "s3cret"}})

    assert session.added[0].payload["arguments"]["password"] == REDACTED


async def test_a_missing_payload_becomes_an_empty_object() -> None:
    """The column is NOT NULL; a None payload must not reach the insert."""
    session = await _record(payload=None)

    assert session.added[0].payload == {}


@pytest.mark.parametrize("field", ["entity_type", "entity_id", "actor_id"])
async def test_optional_attribution_fields_default_to_none(field: str) -> None:
    session = await _record()

    assert getattr(session.added[0], field) is None


async def test_attribution_is_stored_when_supplied() -> None:
    session = await _record(
        actor_id="billing_specialist", entity_type="account", entity_id="acct-1"
    )
    event = session.added[0]

    assert (event.actor_id, event.entity_type, event.entity_id) == (
        "billing_specialist",
        "account",
        "acct-1",
    )
