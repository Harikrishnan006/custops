"""The one way an audit event gets written (BUILD_SPEC §16).

Before this module there were three hand-built ``AuditEvent(...)`` sites and
four of the nineteen event types the specification defines. Adding the missing
fifteen by hand would have meant eighteen places where the payload could carry
chain-of-thought, the actor could be forgotten, or the correlation id could be
dropped — and no single place to fix any of it.

So: one function. Every event goes through ``record_event``, which

* applies :mod:`custops.observability.redaction` to the payload, so no caller
  can persist reasoning or a credential even by accident;
* fills ``execution_id`` and ``request_id`` from the ambient context when the
  caller does not pass them, which is what stops a correlation id going missing
  at exactly the boundaries where it matters most;
* flushes rather than commits, so an audit write joins the caller's transaction
  instead of quietly committing someone else's half-finished work.

The recorder does not decide *whether* an event is interesting. It records what
it is given, redacted and correlated.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from custops.domain.models.audit import AuditEvent
from custops.observability.context import get_execution_id, get_request_id
from custops.observability.events import ActorType, EventType
from custops.observability.logging import get_logger
from custops.observability.redaction import redact

logger = get_logger(__name__)


async def record_event(
    session: AsyncSession,
    event_type: EventType,
    *,
    actor_type: ActorType,
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
    execution_id: uuid.UUID | None = None,
    request_id: str | None = None,
    flush: bool = True,
) -> AuditEvent:
    """Write one audited occurrence into the caller's transaction.

    ``execution_id`` and ``request_id`` fall back to the ambient contextvars.
    Passing them explicitly is for the cases where the ambient context is not
    the right answer — recording an event *about* an execution from outside it,
    as the approval endpoint does.

    ``flush=False`` is for callers already inside a nested transaction that will
    flush anyway; the row is still added to the session.
    """
    event = AuditEvent(
        execution_id=execution_id if execution_id is not None else _ambient_execution_id(),
        request_id=request_id if request_id is not None else get_request_id(),
        event_type=str(event_type),
        actor_type=str(actor_type),
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        # Redaction happens here and nowhere else. A caller that builds its own
        # AuditEvent bypasses this, which is why they no longer do.
        payload=redact(payload or {}),
    )
    session.add(event)
    if flush:
        await session.flush()
    return event


def _ambient_execution_id() -> uuid.UUID | None:
    """The contextvar execution id, as a UUID.

    The contextvar holds a string because it also carries request ids and
    inbound header values, which are not UUIDs. A malformed value yields None
    rather than raising: failing an audit write because a correlation id was
    badly formed would lose the very record that explains what happened.
    """
    raw = get_execution_id()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.warning("audit_execution_id_unparseable", value=raw[:64])
        return None
