"""Per-request correlation context.

Binds a ``request_id`` for the lifetime of each request so every log line emitted
while handling it — including uvicorn's access log and any library logging —
carries the same identifier, and echoes it back so a client can quote it.

An inbound ``X-Request-ID`` is honoured to preserve correlation across a call
chain, but it is untrusted input that lands in log records, so it is sanitized
before use (see ``observability.context.sanitize_correlation_id``).

This middleware does *not* assign an ``execution_id``: in Phase 1 nothing creates
workflows, and inventing an execution per HTTP request would make the field
meaningless when the real workflow runtime arrives in Phase 5.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from custops.observability.context import bind_context, new_id, sanitize_correlation_id
from custops.observability.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind correlation ids around request handling and log the outcome.

    The access log is emitted here rather than left to uvicorn. uvicorn logs at
    the protocol layer, outside the ASGI application and therefore outside this
    context, so its access lines carry a null ``request_id`` — an access log that
    cannot be correlated with the work it describes is exactly the hole §16
    exists to close. ``configure_logging`` disables uvicorn's version so there is
    one access line per request, not two.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request_id = sanitize_correlation_id(inbound) if inbound else new_id()
        started = time.perf_counter()

        with bind_context(request_id=request_id):
            response = await call_next(request)
            logger.info(
                "http_request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
