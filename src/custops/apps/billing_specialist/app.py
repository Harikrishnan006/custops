"""The specialist's A2A surface.

Implements the transport shape the specification defines, verified against the
installed SDK (a2a-sdk 1.x, whose types are protobuf) rather than recalled:

* ``GET /.well-known/agent-card.json`` — discovery
* ``POST /message:send`` — send a message, receive a task
* ``GET /tasks/{id}`` — retrieve a task

Requests arrive as A2A messages whose parts carry the typed payload; responses
come back as tasks carrying an artifact. Task state uses the spec's
``TASK_STATE_*`` names, so a caller written against the protocol — rather than
against this implementation — understands the answer.

**A refusal is a completed exchange with a failed task, not an HTTP 500.** A
specialist that cannot price something has *answered*; only a specialist that
cannot be reached has failed. Collapsing the two would make "this account has no
subscription" indistinguishable from "the process is down", and the caller
routes those very differently — one is a finding, the other is a fallback.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from a2a.types import Role as A2ARole
from a2a.types import TaskState
from fastapi import FastAPI, HTTPException, status
from pydantic import ValidationError

from custops.a2a.contracts.card import AGENT_CARD_PATH, build_agent_card, card_to_json
from custops.a2a.contracts.pricing import (
    CAPABILITY_PRICING_DECISION,
    PricingDecisionRequest,
    PricingRecommendation,
)
from custops.apps.billing_specialist.reasoning import SpecialistRefusalError, price_upgrade
from custops.config import Settings, get_settings
from custops.db.engine import Database, create_database
from custops.observability.context import bind_context
from custops.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)

# Tasks are retained in-process. The specialist answers synchronously, so a task
# exists only long enough for a caller to fetch it by id; persisting them would
# be infrastructure this agent does not need (Rule 7). Bounded so a long-running
# process cannot grow without limit.
MAX_RETAINED_TASKS = 500

STATE_COMPLETED = TaskState.Name(TaskState.TASK_STATE_COMPLETED)
STATE_FAILED = TaskState.Name(TaskState.TASK_STATE_FAILED)
AGENT_ROLE = A2ARole.Name(A2ARole.ROLE_AGENT)


def create_specialist(
    settings: Settings | None = None, database: Database | None = None
) -> FastAPI:
    """Build the specialist's ASGI app.

    ``database`` is injectable so tests drive the real app against a real
    session factory rather than a mocked transport.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)
    db = database if database is not None else create_database(resolved)
    card = build_agent_card(resolved.a2a.billing_specialist_url)
    card_json = card_to_json(card)

    # Per-app, not module-global: two apps in one test process must not share
    # task state, and a module global would also be a data race across the
    # concurrent requests this server accepts.
    tasks: dict[str, dict[str, Any]] = {}

    app = FastAPI(
        title="Billing Specialist",
        description="A2A billing reasoning agent.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(AGENT_CARD_PATH)
    async def agent_card() -> dict[str, Any]:
        """Discovery: a client learns the capabilities from the agent itself."""
        return card_json

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness only — enough for a caller to know the process is up."""
        return {"status": "ok", "agent": card.name}

    @app.post("/message:send")
    async def send_message(payload: dict[str, Any]) -> dict[str, Any]:
        """Accept an A2A message and return a task carrying the answer."""
        message = payload.get("message") or {}
        parts = message.get("parts") or []
        context_id = str(message.get("contextId") or uuid.uuid4())
        task_id = str(uuid.uuid4())

        body = _first_json_part(parts if isinstance(parts, list) else [])
        if body is None:
            return _record(
                tasks,
                _failed_task(
                    task_id,
                    context_id,
                    "invalid_request",
                    "No part carried a JSON pricing request.",
                ),
            )

        try:
            request = PricingDecisionRequest.model_validate(body)
        except ValidationError as error:
            return _record(
                tasks,
                _failed_task(
                    task_id,
                    context_id,
                    "invalid_request",
                    f"Payload does not match the pricing contract "
                    f"({error.error_count()} problem(s)).",
                ),
            )

        # Carry the caller's execution id so the specialist's own tool calls and
        # audit rows land under the same trace (§16). An agent-to-agent hop is
        # exactly where a correlation id otherwise disappears.
        with bind_context(execution_id=str(request.execution_id) if request.execution_id else None):
            logger.info(
                "a2a_pricing_requested",
                account_id=str(request.account_id),
                target_plan=request.target_plan_code,
            )
            task = await _answer(db, request, task_id, context_id)

        return _record(tasks, task)

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        task = tasks.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"No task '{task_id}'."
            )
        return task

    return app


async def _answer(
    db: Database, request: PricingDecisionRequest, task_id: str, context_id: str
) -> dict[str, Any]:
    """Produce the task for one request, refusals included.

    The session is committed because the MCP layer writes ``tool_calls`` and
    ``audit_events`` rows for every call it mediates — including the failed
    ones. A refusal that left no trace would make the specialist's reads
    invisible to the very audit trail the tool boundary exists to produce.
    """
    try:
        async with db.session_factory() as session:
            try:
                recommendation = await price_upgrade(session, request, now=datetime.now(UTC))
            except SpecialistRefusalError as refusal:
                await session.commit()
                return _failed_task(task_id, context_id, refusal.code, refusal.message)
            await session.commit()
    except Exception as error:  # a specialist answers; it does not leak internals
        # Deliberately broad: a driver error's text carries connection details,
        # and an agent reading it would be reading infrastructure it has no
        # business seeing. Log the detail here, return a code.
        logger.exception("a2a_pricing_failed")
        return _failed_task(
            task_id, context_id, "internal_error", f"{type(error).__name__} while pricing."
        )

    logger.info("a2a_pricing_answered", amount_due=str(recommendation.amount_due))
    return _completed_task(task_id, context_id, recommendation)


def _first_json_part(parts: list[Any]) -> dict[str, Any] | None:
    """Find the payload among the message parts.

    A2A parts are heterogeneous by design, so the payload is located rather than
    assumed to sit first. Both a structured ``data`` part and a JSON ``text``
    part are accepted, since clients differ on which they send.
    """
    for part in parts:
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        if isinstance(data, dict):
            return data
        text = part.get("text")
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _record(tasks: dict[str, dict[str, Any]], task: dict[str, Any]) -> dict[str, Any]:
    """Retain a task so ``GET /tasks/{id}`` can answer, bounded by age."""
    while len(tasks) >= MAX_RETAINED_TASKS:
        tasks.pop(next(iter(tasks)))
    tasks[str(task["id"])] = task
    return task


def _completed_task(
    task_id: str, context_id: str, recommendation: PricingRecommendation
) -> dict[str, Any]:
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": STATE_COMPLETED},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": CAPABILITY_PRICING_DECISION,
                "parts": [{"data": recommendation.model_dump(mode="json")}],
            }
        ],
        "history": [{"role": AGENT_ROLE}],
    }


def _failed_task(task_id: str, context_id: str, code: str, message: str) -> dict[str, Any]:
    logger.warning("a2a_pricing_refused", code=code)
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": STATE_FAILED},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "error",
                "parts": [{"data": {"code": code, "message": message}}],
            }
        ],
        "history": [{"role": AGENT_ROLE}],
    }


def main() -> None:  # pragma: no cover - process entry point
    """Run the specialist standalone.

    Independently startable: this needs PostgreSQL, and nothing else in the
    platform. No orchestrator, no API, no portal.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_specialist(settings),
        host=settings.a2a.host,
        port=settings.a2a.port,
    )
