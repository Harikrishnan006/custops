"""The single path every tool call takes.

Permission, approval, audit and error conversion happen **here**, not in each
tool body. That is the point: a tool cannot forget a check it never performs.
Adding a twelfth tool cannot accidentally ship without an audit row, because
writing the audit row is not the tool's job.

Order matters and is deliberate:

1. **Permission** — deny before anything is read or written.
2. **Approval** (mutating tools only) — deny before the mutation.
3. **Handler** — the actual work.
4. **Record** — a ``tool_calls`` row and an ``audit_events`` row, on success and
   on failure alike. A tool call that failed is exactly the kind a trace needs.

Steps 2 and 3 share one **savepoint**, so a handler that raises part-way cannot
leave a half-applied change behind, and cannot leave an approval marked spent
for work that never happened. Step 4 runs outside that savepoint, so a failed
attempt is still on the record after its changes are rolled back.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from custops.domain.models.approval import ToolCall
from custops.mcp.permissions.matrix import (
    PermissionDeniedError,
    ToolPolicy,
    UnknownToolError,
    check_permission,
)
from custops.mcp.tools.approval import ApprovalRequirement, verify_approval
from custops.mcp.tools.results import (
    ToolError,
    ToolErrorCode,
    ToolExecutionError,
    ToolResult,
)
from custops.observability.audit import record_event
from custops.observability.events import ActorType, EventType
from custops.observability.logging import get_logger

logger = get_logger(__name__)

ArgsT = TypeVar("ArgsT", bound=BaseModel)
PayloadT = TypeVar("PayloadT", bound=BaseModel)

Handler = Callable[["ToolContext", Any], Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is calling, on behalf of which workflow."""

    session: AsyncSession
    role: str
    execution_id: uuid.UUID | None = None
    request_id: str | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Outcome:
    """The handler's payload plus the approval it consumed, if any."""

    payload: BaseModel
    approval_id: uuid.UUID | None


@dataclass
class _Invocation:
    """Did the handler actually run?

    Plain Python state, deliberately. The handler runs inside a savepoint, so an
    audit row written next to it would be rolled back when the handler raises —
    and "the tool was called and then failed" is precisely the fact a trace most
    needs to keep. Recording it in memory and writing it afterwards, outside the
    savepoint, is what makes ``tool_called`` survive the rollback.
    """

    called: bool = False


async def execute_tool(
    context: ToolContext,
    tool: str,
    arguments: BaseModel,
    handler: Handler,
    *,
    approval_entity: tuple[str, str] | None = None,
    approval_action: str | None = None,
    consume_approval: bool = True,
) -> ToolResult[Any]:
    """Run one tool call through permission, approval, execution and audit.

    ``approval_entity`` is the (entity_type, entity_id) the mutation targets. A
    mutating tool that does not supply it is refused rather than defaulted: an
    approval that isn't scoped to a specific entity authorises every entity.

    ``approval_action`` overrides the action name this call verifies against.
    One workflow performs several mutations under a single human decision — a
    person approves *the upgrade*, not three technical steps — so each tool in
    that workflow verifies the same approval rather than requiring its own.

    ``consume_approval`` controls whether this call spends it. When several
    tools share one approval, only the last should consume; spending it on the
    first would leave the rest unauthorised mid-workflow, with billing changed
    and provisioning refused. The caller is responsible for consuming exactly
    once — verification still happens on every call, which is what D9 requires.
    """
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    invocation = _Invocation()

    # The agent chose this tool. Recorded before permission is even checked, so
    # a denied attempt still shows what was attempted — which is the whole point
    # of auditing a refusal.
    await record_event(
        context.session,
        EventType.TOOL_SELECTED,
        actor_type=ActorType.AGENT,
        actor_id=context.role,
        entity_type="tool",
        entity_id=tool,
        payload={"arguments": _safe_dump(arguments)},
        execution_id=context.execution_id,
        request_id=context.request_id,
    )

    try:
        policy = check_permission(context.role, tool)
        async with context.session.begin_nested():
            outcome = await _authorise_and_run(
                context,
                tool,
                policy,
                arguments,
                handler,
                approval_entity,
                approval_action=approval_action,
                consume_approval=consume_approval,
                invocation=invocation,
            )

    except PermissionDeniedError as error:
        return await _fail(
            context,
            tool,
            arguments,
            ToolErrorCode.PERMISSION_DENIED,
            str(error),
            started,
            started_at,
            invocation,
        )
    except UnknownToolError as error:
        return await _fail(
            context,
            tool,
            arguments,
            ToolErrorCode.INVALID_INPUT,
            str(error),
            started,
            started_at,
            invocation,
        )
    except ToolExecutionError as error:
        return await _fail(
            context,
            tool,
            arguments,
            error.code,
            error.message,
            started,
            started_at,
            invocation,
            details=error.details,
        )
    except Exception as error:  # a tool never raises at its boundary
        # Deliberately broad. An unhandled exception escaping into an agent's
        # context is both a leak (driver messages carry connection details) and
        # an invitation to improvise. Log the detail, return a code.
        logger.exception("tool_unhandled_error", tool=tool)
        return await _fail(
            context,
            tool,
            arguments,
            ToolErrorCode.INTERNAL_ERROR,
            f"{type(error).__name__} while executing '{tool}'.",
            started,
            started_at,
            invocation,
        )

    await _record(
        context,
        tool=tool,
        arguments=arguments,
        result=outcome.payload,
        succeeded=True,
        error_code=None,
        error_message=None,
        started=started,
        started_at=started_at,
        approval_id=outcome.approval_id,
        invocation=invocation,
    )
    return ToolResult(ok=True, data=outcome.payload)


async def _authorise_and_run(
    context: ToolContext,
    tool: str,
    policy: ToolPolicy,
    arguments: BaseModel,
    handler: Handler,
    approval_entity: tuple[str, str] | None,
    *,
    approval_action: str | None = None,
    consume_approval: bool = True,
    invocation: _Invocation,
) -> _Outcome:
    """Verify approval where required, then run the handler."""
    approval_id: uuid.UUID | None = None

    if policy.mutating:
        if context.execution_id is None:
            raise ToolExecutionError(
                ToolErrorCode.APPROVAL_REQUIRED,
                f"Tool '{tool}' mutates state and requires an execution_id to verify "
                "approval against.",
            )
        if approval_entity is None:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_INPUT,
                f"Tool '{tool}' mutates state but supplied no target entity; an "
                "unscoped approval would authorise every entity.",
            )
        approval = await verify_approval(
            context.session,
            ApprovalRequirement(
                execution_id=context.execution_id,
                action=approval_action or policy.approval_action or tool,
                entity_type=approval_entity[0],
                entity_id=approval_entity[1],
            ),
            consume=consume_approval,
        )
        approval_id = approval.id

    # Marked before the await, so a handler that raises is still recorded as
    # having been called.
    invocation.called = True
    payload = await handler(context, arguments)
    return _Outcome(payload=payload, approval_id=approval_id)


async def _fail(
    context: ToolContext,
    tool: str,
    arguments: BaseModel,
    code: ToolErrorCode,
    message: str,
    started: float,
    started_at: datetime,
    invocation: _Invocation,
    details: dict[str, Any] | None = None,
) -> ToolResult[Any]:
    await _record(
        context,
        tool=tool,
        arguments=arguments,
        result=None,
        succeeded=False,
        error_code=code,
        error_message=message,
        started=started,
        started_at=started_at,
        approval_id=None,
        invocation=invocation,
    )
    logger.warning("tool_failed", tool=tool, code=str(code), role=context.role)
    return ToolResult(ok=False, error=ToolError(code=code, message=message, details=details or {}))


async def _record(
    context: ToolContext,
    *,
    tool: str,
    arguments: BaseModel,
    result: BaseModel | None,
    succeeded: bool,
    error_code: str | None,
    error_message: str | None,
    started: float,
    started_at: datetime,
    approval_id: uuid.UUID | None,
    invocation: _Invocation,
) -> None:
    """Write the tool_calls and audit_events rows (§8).

    Both are written for failures too — a trace that only records successes
    cannot answer "what did it try?", which is the first question asked when a
    workflow ends somewhere unexpected.
    """
    duration_ms = int((time.perf_counter() - started) * 1000)

    context.session.add(
        ToolCall(
            execution_id=context.execution_id,
            tool_name=tool,
            arguments=_safe_dump(arguments),
            result=_safe_dump(result) if result is not None else None,
            succeeded=succeeded,
            error_code=error_code,
            error_message=error_message,
            approval_id=approval_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=duration_ms,
        )
    )

    # Written here rather than beside the handler because the handler runs in a
    # savepoint: a row written there vanishes when the handler raises, losing
    # exactly the fact that the tool *was* called before it failed. Emitted
    # before the completion event so insertion order matches causal order.
    if invocation.called:
        await record_event(
            context.session,
            EventType.TOOL_CALLED,
            actor_type=ActorType.AGENT,
            actor_id=context.role,
            entity_type="tool",
            entity_id=tool,
            payload={"started_at": started_at.isoformat()},
            execution_id=context.execution_id,
            request_id=context.request_id,
        )

    await record_event(
        context.session,
        EventType.TOOL_COMPLETED,
        actor_type=ActorType.AGENT,
        actor_id=context.role,
        entity_type="tool",
        entity_id=tool,
        payload={
            "succeeded": succeeded,
            "error_code": error_code,
            "duration_ms": duration_ms,
        },
        execution_id=context.execution_id,
        request_id=context.request_id,
    )


def _safe_dump(model: BaseModel) -> dict[str, Any]:
    """Serialise for storage.

    ``mode="json"`` so UUIDs, Decimals and datetimes land as JSON scalars rather
    than failing the JSONB insert at commit time — a failure that would surface
    far from its cause.
    """
    return model.model_dump(mode="json")
