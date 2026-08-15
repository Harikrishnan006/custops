"""Dependency liveness probing.

A probe answers one question: *can this process actually reach this dependency
right now?* Two rules make the answer trustworthy:

1. **Live, never cached.** A probe performs a real operation on every call. A
   flag set at startup tells you the dependency was reachable once, which is not
   what a health endpoint is asked.
2. **Bounded.** Every probe runs under a timeout. An unbounded probe converts a
   dead dependency into a hanging request, and most orchestrators read a hang as
   healthy until the client gives up.

Probe results are infrastructure-level values; the API layer maps them to a
transport schema (``apps/api/schemas/health.py``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Error text is surfaced in the /health body and in logs. Truncate so a verbose
# driver exception cannot dominate the response or the log stream.
_MAX_ERROR_LENGTH = 200


class ProbeStatus(StrEnum):
    """Reachability of a single dependency."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one dependency probe."""

    name: str
    status: ProbeStatus
    latency_ms: float
    detail: dict[str, Any] | None = None
    error: str | None = None

    @property
    def is_up(self) -> bool:
        return self.status is ProbeStatus.UP


def _describe_error(exc: BaseException) -> str:
    """Render an exception as a short, log-safe string."""
    text = f"{type(exc).__name__}: {exc}".strip()
    if len(text) > _MAX_ERROR_LENGTH:
        return text[: _MAX_ERROR_LENGTH - 1] + "…"
    return text


async def run_probe(
    name: str,
    operation: Callable[[], Awaitable[dict[str, Any] | None]],
    *,
    # ASYNC109 discourages timeout parameters in favour of caller-side
    # asyncio.timeout. Suppressed deliberately: the bound is applied here with
    # asyncio.wait_for, and making it a parameter is what guarantees *every*
    # probe is bounded rather than hoping each call site remembers to wrap it.
    timeout: float,  # noqa: ASYNC109
) -> ProbeResult:
    """Run ``operation`` under a timeout and convert any failure into a result.

    A probe never raises. Health checking is precisely the code path that must
    not fail loudly, because its whole job is to report failure calmly.
    """
    started = time.perf_counter()
    try:
        detail = await asyncio.wait_for(operation(), timeout=timeout)
    except TimeoutError:
        return ProbeResult(
            name=name,
            status=ProbeStatus.DOWN,
            latency_ms=_elapsed_ms(started),
            error=f"probe exceeded {timeout:g}s timeout",
        )
    except Exception as exc:  # deliberately broad: see docstring
        return ProbeResult(
            name=name,
            status=ProbeStatus.DOWN,
            latency_ms=_elapsed_ms(started),
            error=_describe_error(exc),
        )
    return ProbeResult(
        name=name,
        status=ProbeStatus.UP,
        latency_ms=_elapsed_ms(started),
        detail=detail,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
