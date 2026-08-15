"""Tool results: structured failures, never raw exceptions.

BUILD_SPEC §8 requires tool errors to return structured failures. The reason is
not tidiness — an agent receiving a stack trace has no way to distinguish "the
customer does not exist" (replan) from "the database is unreachable" (retry)
from "you are not allowed to do that" (escalate). A traceback invites the model
to improvise; a typed error code lets the graph route deterministically.

A raw exception is also a leak: driver messages carry connection strings, table
names, and occasionally data. What reaches an agent's context should be a code
and a sentence, not the internals of the failure.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

PayloadT = TypeVar("PayloadT", bound=BaseModel)


class ToolErrorCode(StrEnum):
    """Why a tool call failed, in terms the graph can route on."""

    # The caller may not do this. Escalate; never retry.
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_NOT_GRANTED = "approval_not_granted"
    APPROVAL_ALREADY_CONSUMED = "approval_already_consumed"

    # The request was wrong. Replan; retrying the same call cannot help.
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    PRECONDITION_FAILED = "precondition_failed"

    # The world was uncooperative. Retry may help.
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_ERROR = "upstream_error"

    # Anything unclassified. Treated as non-retryable: guessing that an unknown
    # failure is transient is how a workflow retries a corruption into place.
    INTERNAL_ERROR = "internal_error"


# Codes where trying the identical call again is meaningful. Everything else
# needs a different call, a human, or a different plan.
RETRYABLE_CODES = frozenset({ToolErrorCode.UPSTREAM_TIMEOUT, ToolErrorCode.UPSTREAM_ERROR})


class ToolError(BaseModel):
    """A failure an agent can reason about."""

    code: ToolErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class ToolResult(BaseModel, Generic[PayloadT]):
    """Every tool returns one of these — success or failure, never an exception.

    Generic over the payload so each tool declares its own output schema
    (§8: every tool has a Pydantic input schema and a Pydantic output schema).
    """

    ok: bool
    data: PayloadT | None = None
    error: ToolError | None = None

    @classmethod
    def success(cls, data: PayloadT) -> ToolResult[PayloadT]:
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: ToolErrorCode,
        message: str,
        **details: Any,
    ) -> ToolResult[PayloadT]:
        return cls(ok=False, error=ToolError(code=code, message=message, details=details))


class ToolExecutionError(Exception):
    """Internal signal, converted to a ``ToolResult`` at the tool boundary.

    Exists so tool bodies can fail with a code without every helper threading a
    result object back up. It never escapes the tool layer.
    """

    def __init__(self, code: ToolErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
