"""Health response schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from custops.observability.probes import ProbeResult, ProbeStatus


class DependencyStatus(BaseModel):
    """Reported state of one dependency."""

    status: ProbeStatus = Field(description="Whether this dependency answered.")
    latency_ms: float = Field(description="Round-trip time of the probe.")
    detail: dict[str, Any] | None = Field(
        default=None,
        description="Dependency-specific facts, e.g. server version, pgvector version.",
    )
    error: str | None = Field(
        default=None,
        description="Short failure description when the probe did not succeed.",
    )

    @classmethod
    def from_probe(cls, result: ProbeResult) -> DependencyStatus:
        return cls(
            status=result.status,
            latency_ms=result.latency_ms,
            detail=result.detail,
            error=result.error,
        )


class HealthResponse(BaseModel):
    """Aggregate health of this service and its dependencies.

    ``status`` is derived: ``ok`` only when every dependency answered. The body is
    identical whether the HTTP status is 200 or 503, so a caller that ignores the
    status code still sees which dependency failed and why.
    """

    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    request_id: str | None = Field(
        default=None,
        description="Correlation id for this request; echoed in the X-Request-ID header.",
    )
    execution_id: str | None = Field(
        default=None,
        description=(
            "Workflow execution this call belongs to. Always null in Phase 1 — "
            "no workflow runtime exists yet."
        ),
    )
    dependencies: dict[str, DependencyStatus]
