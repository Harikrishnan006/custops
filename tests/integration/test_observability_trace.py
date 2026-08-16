"""Do the events actually land, and does the endpoint reconstruct them (§16)?

The unit suite proves the taxonomy is wired and the assembler orders correctly.
Neither can prove a row reaches PostgreSQL, that the ordering rule holds against
a real ``now()`` default, or that the endpoint serves a complete trace. That is
what these do.

Infrastructure-dependent; pending until PostgreSQL with pgvector is available.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from custops.db.engine import Database
from custops.domain.models.audit import AuditEvent
from custops.domain.seed import clear_seed_data, seed_all
from custops.observability.audit import record_event
from custops.observability.context import bind_context
from custops.observability.events import ActorType, EventType
from custops.observability.redaction import DROPPED_MARKER, REDACTED
from custops.observability.trace import build_timeline
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


async def _events_for(database: Database, execution_id: uuid.UUID) -> list[AuditEvent]:
    async with database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.execution_id == execution_id)
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            ).scalars()
        )


class TestTheRecorderAgainstPostgres:
    async def test_an_event_is_persisted_and_readable(self, database: Database) -> None:
        execution_id = uuid.uuid4()

        async with database.session_factory() as session:
            await record_event(
                session,
                EventType.REQUEST_RECEIVED,
                actor_type=ActorType.USER,
                entity_type="workflow_execution",
                entity_id=str(execution_id),
                payload={"raw_request": "Upgrade ACME."},
                execution_id=execution_id,
            )
            await session.commit()

        events = await _events_for(database, execution_id)

        assert [event.event_type for event in events] == [str(EventType.REQUEST_RECEIVED)]
        assert events[0].payload["raw_request"] == "Upgrade ACME."

    async def test_redaction_survives_the_round_trip(self, database: Database) -> None:
        """The prohibition has to hold in the column, not just in memory."""
        execution_id = uuid.uuid4()

        async with database.session_factory() as session:
            await record_event(
                session,
                EventType.DECISION_MADE,
                actor_type=ActorType.AGENT,
                payload={"reasoning": "LEAKED", "password": "s3cret", "outcome": "eligible"},
                execution_id=execution_id,
            )
            await session.commit()

        stored = (await _events_for(database, execution_id))[0].payload

        assert "LEAKED" not in str(stored)
        assert "s3cret" not in str(stored)
        assert stored["password"] == REDACTED
        assert stored[DROPPED_MARKER] == ["reasoning"]
        assert stored["outcome"] == "eligible"

    async def test_rows_written_in_one_transaction_share_a_timestamp(
        self, database: Database
    ) -> None:
        """The premise behind the ordering rule, verified against real PostgreSQL.

        ``now()`` is transaction time. If this ever stops being true the
        ``(occurred_at, id)`` ordering is merely redundant rather than wrong —
        but the assumption should be checked, not believed.
        """
        execution_id = uuid.uuid4()

        async with database.session_factory() as session:
            for event in (
                EventType.TOOL_SELECTED,
                EventType.TOOL_CALLED,
                EventType.TOOL_COMPLETED,
            ):
                await record_event(
                    session, event, actor_type=ActorType.AGENT, execution_id=execution_id
                )
            await session.commit()

        events = await _events_for(database, execution_id)

        assert len({event.occurred_at for event in events}) == 1
        assert [event.event_type for event in events] == [
            str(EventType.TOOL_SELECTED),
            str(EventType.TOOL_CALLED),
            str(EventType.TOOL_COMPLETED),
        ]

    async def test_the_monotonic_id_orders_a_shared_timestamp(self, database: Database) -> None:
        """The tie-break the trace endpoint depends on."""
        execution_id = uuid.uuid4()

        async with database.session_factory() as session:
            for index in range(5):
                await record_event(
                    session,
                    EventType.RETRY,
                    actor_type=ActorType.SYSTEM,
                    payload={"n": index},
                    execution_id=execution_id,
                )
            await session.commit()

        events = await _events_for(database, execution_id)
        timeline = build_timeline(steps=[], tool_calls=[], events=events)

        assert [entry.detail["payload"]["n"] for entry in timeline] == [0, 1, 2, 3, 4]

    async def test_the_ambient_execution_id_reaches_the_column(self, database: Database) -> None:
        execution_id = uuid.uuid4()

        async with database.session_factory() as session:
            with bind_context(execution_id=str(execution_id), request_id="req-obs"):
                await record_event(session, EventType.PLAN_CREATED, actor_type=ActorType.AGENT)
            await session.commit()

        events = await _events_for(database, execution_id)

        assert events[0].execution_id == execution_id
        assert events[0].request_id == "req-obs"

    async def test_an_audit_row_is_not_bound_to_the_execution_it_names(
        self, database: Database
    ) -> None:
        """``execution_id`` is a correlation key, not a foreign key.

        An audit write must never fail because the row it describes is absent —
        that would lose the record precisely when something has gone wrong.
        """
        orphan = uuid.uuid4()

        async with database.session_factory() as session:
            await record_event(
                session,
                EventType.WORKFLOW_FAILED,
                actor_type=ActorType.SYSTEM,
                execution_id=orphan,
            )
            await session.commit()

        assert len(await _events_for(database, orphan)) == 1


class TestTheInspectionEndpoint:
    async def test_a_missing_execution_is_a_404(self, operator_client: object) -> None:
        from httpx import AsyncClient

        assert isinstance(operator_client, AsyncClient)
        response = await operator_client.get(f"/workflows/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_the_trace_exposes_a_merged_timeline(
        self, seeded: Database, operator_client: object
    ) -> None:
        """The unified representation §16 asks for."""
        from httpx import AsyncClient

        assert isinstance(operator_client, AsyncClient)

        started = await operator_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        assert started.status_code in (200, 201, 202), started.text
        execution_id = started.json()["execution_id"]

        trace = await operator_client.get(f"/workflows/{execution_id}")

        assert trace.status_code == 200
        body = trace.json()
        assert body["timeline"], "the trace exposed no timeline"
        assert body["event_coverage"]["emitted"]
        # request_received is written before the graph runs, so it is present
        # for every execution regardless of how the run turned out.
        assert str(EventType.REQUEST_RECEIVED) in body["event_coverage"]["emitted"]

    async def test_the_timeline_is_ordered(self, seeded: Database, operator_client: object) -> None:
        from httpx import AsyncClient

        assert isinstance(operator_client, AsyncClient)

        started = await operator_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        execution_id = started.json()["execution_id"]

        body = (await operator_client.get(f"/workflows/{execution_id}")).json()
        timestamps = [entry["at"] for entry in body["timeline"]]

        assert timestamps == sorted(timestamps)

    async def test_the_endpoint_never_serves_chain_of_thought(
        self, seeded: Database, operator_client: object
    ) -> None:
        """The security requirement, checked at the boundary that discloses.

        A trace endpoint is the one place where everything the platform recorded
        becomes visible at once, so the prohibition is verified against the
        serialised response rather than against any single row.
        """
        from httpx import AsyncClient

        assert isinstance(operator_client, AsyncClient)

        started = await operator_client.post(
            "/workflows", json={"raw_request": "Upgrade ACME to enterprise."}
        )
        execution_id = started.json()["execution_id"]

        raw = (await operator_client.get(f"/workflows/{execution_id}")).text.lower()

        for forbidden in ("chain_of_thought", "scratchpad", "inner_monologue", "raw_completion"):
            assert forbidden not in raw
