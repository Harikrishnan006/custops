"""Ambient correlation identifiers.

Two identifiers travel with work through the system:

``execution_id``
    One workflow execution. Assigned when a workflow starts and carried through
    every agent run, tool call, A2A request, validation and audit event so a
    full trace can be reconstructed afterwards (BUILD_SPEC §16).

``request_id``
    One inbound HTTP request. Shorter-lived than an execution; useful for
    correlating an API call with the workflow it triggered.

Phase 1 deliberately never assigns an ``execution_id`` — nothing creates
workflows yet. The contextvar and the log field exist now so that the
propagation seam is *one* place when Phase 5 introduces the LangGraph runtime,
rather than a change threaded through every call site. Phase 1's definition of
done requires the field to be present in the log schema for exactly this reason.

ContextVars (not thread locals) because the whole runtime is asyncio: each task
inherits a copy of the context, so concurrent workflows cannot read each other's
identifiers.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

EXECUTION_ID_KEY = "execution_id"
REQUEST_ID_KEY = "request_id"

_execution_id: ContextVar[str | None] = ContextVar("custops_execution_id", default=None)
_request_id: ContextVar[str | None] = ContextVar("custops_request_id", default=None)

_MAX_ID_LENGTH = 64
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]")


def get_execution_id() -> str | None:
    """Return the current execution id, or ``None`` outside a workflow."""
    return _execution_id.get()


def get_request_id() -> str | None:
    """Return the current request id, or ``None`` outside a request."""
    return _request_id.get()


def new_id() -> str:
    """Generate a fresh correlation identifier."""
    return str(uuid.uuid4())


def sanitize_correlation_id(value: str) -> str:
    """Make a caller-supplied identifier safe to put in logs.

    Correlation ids can arrive from an inbound header, which makes them
    untrusted input that ends up in log records. Restrict the character set and
    length so a caller cannot inject newlines or control characters into the log
    stream.
    """
    cleaned = _UNSAFE_ID_CHARS.sub("", value.strip())[:_MAX_ID_LENGTH]
    return cleaned or new_id()


@contextmanager
def bind_context(
    *,
    execution_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation identifiers for the duration of the block.

    Only the identifiers explicitly passed are bound; the others keep whatever
    value the surrounding context has. Tokens are reset in a ``finally`` so a
    raised exception cannot leak an identifier into unrelated work.
    """
    execution_token = _execution_id.set(execution_id) if execution_id is not None else None
    request_token = _request_id.set(request_id) if request_id is not None else None
    try:
        yield
    finally:
        if request_token is not None:
            _request_id.reset(request_token)
        if execution_token is not None:
            _execution_id.reset(execution_token)
