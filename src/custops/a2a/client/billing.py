"""Consulting the Billing Specialist, and coping when it isn't there.

The orchestrator holds a **URL**, not an import. That is the substance of D6:
the specialist could be rewritten in another language or moved to another host
and nothing here would change.

Three outcomes, kept distinct because the workflow treats them differently:

``consulted``
    The specialist answered. Its figures are compared with the local ones and
    the comparison is recorded.
``refused``
    The specialist answered "I cannot price this". A finding — something is
    genuinely odd about the account — worth recording, not worth halting for.
``unavailable``
    The specialist could not be reached at all. Not an answer, and explicitly
    not a blocker: the workflow proceeds on the local deterministic calculation
    and records that it did.

Flattening ``refused`` into ``unavailable`` would let a real data problem hide
behind "the optional service was down", which is the failure mode that makes
optional dependencies dangerous.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import httpx
from pydantic import ValidationError

from custops.a2a.contracts.card import AGENT_CARD_PATH
from custops.a2a.contracts.pricing import (
    CAPABILITY_PRICING_DECISION,
    PricingDecisionRequest,
    PricingRecommendation,
)
from custops.observability.logging import get_logger

logger = get_logger(__name__)

MESSAGE_SEND_PATH = "/message:send"
COMPLETED_STATE = "TASK_STATE_COMPLETED"


class ConsultStatus(StrEnum):
    CONSULTED = "consulted"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ConsultResult:
    """What came back, in a shape the workflow can record verbatim."""

    status: ConsultStatus
    recommendation: PricingRecommendation | None = None
    # Why the specialist refused, or why it could not be reached.
    detail: str | None = None

    @property
    def answered(self) -> bool:
        return self.recommendation is not None

    def as_trace(self) -> dict[str, Any]:
        """A structured summary for the workflow trace (§16, Rule 18)."""
        trace: dict[str, Any] = {"status": str(self.status)}
        if self.detail:
            trace["detail"] = self.detail
        if self.recommendation is not None:
            trace["amount_due"] = str(self.recommendation.amount_due)
            trace["billing_eligible"] = self.recommendation.billing_eligible
            trace["approval_indicated"] = self.recommendation.approval_indicated
            trace["confidence"] = self.recommendation.confidence
            trace["rationale_summary"] = self.recommendation.rationale_summary
        return trace


class BillingSpecialistClient:
    """An A2A client for the pricing capability.

    Deliberately narrow: it speaks the two calls this capability needs rather
    than wrapping the whole protocol. A general-purpose client here would invite
    the orchestrator to use the specialist for things it never agreed to do.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # A seam, not a mock: tests pass an ASGI transport so this exact client
        # code runs against the real specialist app, and a mock transport to
        # simulate the network failures a live server cannot be made to produce
        # on demand. Production leaves it None and speaks over a socket.
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def fetch_card(self) -> dict[str, Any] | None:
        """Read the published agent card, or ``None`` if unreachable."""
        try:
            async with self._http() as client:
                response = await client.get(f"{self._base_url}{AGENT_CARD_PATH}")
                response.raise_for_status()
                card: dict[str, Any] = response.json()
                return card
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("a2a_card_unavailable", error=type(error).__name__)
            return None

    async def request_pricing_decision(
        self,
        *,
        account_id: uuid.UUID,
        target_plan_code: str,
        execution_id: uuid.UUID | None = None,
    ) -> ConsultResult:
        """Ask the specialist to price an upgrade.

        Every transport failure becomes ``unavailable`` rather than an
        exception. A second opinion that can take down the workflow when it is
        offline is not optional, whatever the configuration says.
        """
        request = PricingDecisionRequest(
            account_id=account_id,
            target_plan_code=target_plan_code,
            execution_id=execution_id,
        )
        envelope = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "contextId": str(execution_id) if execution_id else None,
                "parts": [{"data": request.model_dump(mode="json")}],
            }
        }

        try:
            async with self._http() as client:
                response = await client.post(f"{self._base_url}{MESSAGE_SEND_PATH}", json=envelope)
                response.raise_for_status()
                task = response.json()
        except httpx.HTTPError as error:
            logger.warning("a2a_specialist_unavailable", error=type(error).__name__)
            return ConsultResult(
                status=ConsultStatus.UNAVAILABLE,
                detail=f"{type(error).__name__} contacting the billing specialist.",
            )
        except ValueError:
            return ConsultResult(
                status=ConsultStatus.UNAVAILABLE,
                detail="Billing specialist returned a non-JSON response.",
            )

        return _interpret(task)


def _interpret(task: Any) -> ConsultResult:
    """Turn a task into a result, distrusting its shape throughout.

    This payload crossed a process boundary, so every field is checked rather
    than assumed. A malformed body from a healthy-looking response is treated as
    unavailability: the orchestrator has no answer either way, and guessing at a
    partial payload is how a wrong amount reaches an invoice.
    """
    if not isinstance(task, dict):
        return ConsultResult(
            status=ConsultStatus.UNAVAILABLE, detail="Task payload was not an object."
        )

    state = (task.get("status") or {}).get("state")
    artifacts = task.get("artifacts") or []
    payload = _first_artifact_data(artifacts)

    if state != COMPLETED_STATE:
        detail = "Specialist reported a failed task."
        if isinstance(payload, dict) and payload.get("message"):
            code = payload.get("code", "unknown")
            detail = f"{code}: {payload['message']}"
        return ConsultResult(status=ConsultStatus.REFUSED, detail=detail)

    if payload is None:
        return ConsultResult(
            status=ConsultStatus.UNAVAILABLE,
            detail="Completed task carried no recommendation artifact.",
        )

    try:
        recommendation = PricingRecommendation.model_validate(payload)
    except ValidationError as error:
        logger.warning("a2a_recommendation_invalid", problems=error.error_count())
        return ConsultResult(
            status=ConsultStatus.UNAVAILABLE,
            detail=f"Recommendation failed contract validation ({error.error_count()} problem(s)).",
        )

    return ConsultResult(status=ConsultStatus.CONSULTED, recommendation=recommendation)


def _first_artifact_data(artifacts: Any) -> dict[str, Any] | None:
    """Pull the data part out of the first artifact that carries one."""
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("name") not in {CAPABILITY_PRICING_DECISION, "error"}:
            continue
        for part in artifact.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("data"), dict):
                data: dict[str, Any] = part["data"]
                return data
    return None


def corroborates(result: ConsultResult, *, local_amount: Decimal) -> tuple[bool, str | None]:
    """Compare the specialist's figure with the locally computed one.

    Returns whether the two agree and, if not, a description of the divergence.

    **The local figure always wins.** The specialist is corroboration, not an
    authority: an out-of-process agent that could change the amount charged
    would move the money decision outside the audited deterministic path. A
    disagreement is recorded loudly and the local figure is used — because a
    divergence means the two sides read different state, and the side that also
    performs the mutation is the one whose reading must govern it.
    """
    if result.recommendation is None:
        return False, None
    remote = result.recommendation.amount_due
    if remote == local_amount:
        return True, None
    return False, (
        f"Specialist priced {remote} against local {local_amount}; using the local figure."
    )
