"""The orchestrator's side of the A2A boundary.

The interesting behaviour is not the happy path — it is the three-way split
between *answered*, *refused* and *unreachable*, and the rule that the local
figure always governs. These tests drive the real client over a real HTTP
transport rather than patching its internals, so the request it actually
composes is the one under test.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import httpx

from custops.a2a.client.billing import (
    BillingSpecialistClient,
    ConsultStatus,
    corroborates,
)
from custops.a2a.contracts.card import AGENT_CARD_PATH
from custops.a2a.contracts.pricing import CAPABILITY_PRICING_DECISION

BASE_URL = "http://specialist.test"
ACCOUNT_ID = uuid.uuid4()

RECOMMENDATION: dict[str, Any] = {
    "account_id": str(ACCOUNT_ID),
    "current_plan_code": "professional",
    "target_plan_code": "enterprise",
    "amount_due": "500.00",
    "currency": "USD",
    "unused_credit": "250.00",
    "new_plan_charge": "750.00",
    "days_remaining": 15,
    "days_in_period": 30,
    "billing_eligible": True,
    "billing_blockers": [],
    "approval_indicated": False,
    "approval_triggers": [],
    "confidence": 0.9,
    "rationale_summary": "Prorated for the remainder of the period.",
    "evidence_refs": ["subscription:abc"],
}


def _completed(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "contextId": str(uuid.uuid4()),
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": CAPABILITY_PRICING_DECISION,
                "parts": [{"data": payload if payload is not None else RECOMMENDATION}],
            }
        ],
    }


def _failed(code: str, message: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "contextId": str(uuid.uuid4()),
        "status": {"state": "TASK_STATE_FAILED"},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "error",
                "parts": [{"data": {"code": code, "message": message}}],
            }
        ],
    }


def _client_returning(
    body: Any, *, status_code: int = 200, capture: list[httpx.Request] | None = None
) -> BillingSpecialistClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status_code, json=body)

    return BillingSpecialistClient(BASE_URL, transport=httpx.MockTransport(handler))


def _client_raising(error: Exception) -> BillingSpecialistClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return BillingSpecialistClient(BASE_URL, transport=httpx.MockTransport(handler))


async def _consult(client: BillingSpecialistClient) -> Any:
    return await client.request_pricing_decision(
        account_id=ACCOUNT_ID, target_plan_code="enterprise"
    )


# ------------------------------------------------------------------- answered


async def test_a_completed_task_yields_the_validated_recommendation() -> None:
    result = await _consult(_client_returning(_completed()))

    assert result.status is ConsultStatus.CONSULTED
    assert result.recommendation is not None
    assert result.recommendation.amount_due == Decimal("500.00")
    assert result.recommendation.billing_eligible is True


async def test_the_request_sends_identifiers_in_an_a2a_message_envelope() -> None:
    """What the client puts on the wire is part of the contract."""
    captured: list[httpx.Request] = []
    execution_id = uuid.uuid4()

    client = _client_returning(_completed(), capture=captured)
    await client.request_pricing_decision(
        account_id=ACCOUNT_ID, target_plan_code="enterprise", execution_id=execution_id
    )

    assert str(captured[0].url) == f"{BASE_URL}/message:send"
    body = json.loads(captured[0].content)
    message = body["message"]
    assert message["role"] == "ROLE_USER"
    # The execution id travels as the A2A context id, so the specialist's own
    # tool calls land under the caller's trace (§16).
    assert message["contextId"] == str(execution_id)
    assert message["parts"][0]["data"] == {
        "account_id": str(ACCOUNT_ID),
        "target_plan_code": "enterprise",
        "execution_id": str(execution_id),
    }


# -------------------------------------------------------------------- refused


async def test_a_failed_task_is_a_refusal_not_an_outage() -> None:
    """The specialist answered; the answer was 'I cannot price this'.

    Keeping this distinct from unavailability is the whole point: a real data
    problem must not hide behind "the optional service was down".
    """
    result = await _consult(
        _client_returning(_failed("no_active_subscription", "Account has none."))
    )

    assert result.status is ConsultStatus.REFUSED
    assert result.recommendation is None
    assert result.detail is not None
    assert "no_active_subscription" in result.detail
    assert "Account has none." in result.detail


# ---------------------------------------------------------------- unreachable


async def test_a_connection_error_is_unavailable_and_never_raises() -> None:
    """A second opinion that can take down the workflow is not optional."""
    result = await _consult(_client_raising(httpx.ConnectError("refused")))

    assert result.status is ConsultStatus.UNAVAILABLE
    assert result.recommendation is None
    assert result.detail is not None
    assert "ConnectError" in result.detail


async def test_a_timeout_is_unavailable() -> None:
    result = await _consult(_client_raising(httpx.ReadTimeout("too slow")))
    assert result.status is ConsultStatus.UNAVAILABLE


async def test_a_server_error_is_unavailable() -> None:
    """A 500 is the transport failing, not the agent answering."""
    result = await _consult(_client_returning({"detail": "boom"}, status_code=500))
    assert result.status is ConsultStatus.UNAVAILABLE


async def test_a_malformed_recommendation_is_not_treated_as_an_answer() -> None:
    """A healthy-looking response carrying a broken body is still no answer.

    Guessing at a partial payload is how a wrong amount reaches an invoice.
    """
    broken = {**RECOMMENDATION}
    del broken["amount_due"]

    result = await _consult(_client_returning(_completed(broken)))

    assert result.status is ConsultStatus.UNAVAILABLE
    assert result.recommendation is None


async def test_a_completed_task_with_no_artifact_is_not_an_answer() -> None:
    result = await _consult(
        _client_returning({"id": "t", "status": {"state": "TASK_STATE_COMPLETED"}})
    )
    assert result.status is ConsultStatus.UNAVAILABLE


async def test_a_non_object_body_is_not_an_answer() -> None:
    result = await _consult(_client_returning(["not", "a", "task"]))
    assert result.status is ConsultStatus.UNAVAILABLE


async def test_an_unreachable_card_returns_none_rather_than_raising() -> None:
    client = _client_raising(httpx.ConnectError("refused"))
    assert await client.fetch_card() is None


async def test_the_card_is_fetched_from_the_well_known_path() -> None:
    captured: list[httpx.Request] = []
    client = _client_returning({"name": "billing-specialist"}, capture=captured)

    card = await client.fetch_card()

    assert card == {"name": "billing-specialist"}
    assert str(captured[0].url) == f"{BASE_URL}{AGENT_CARD_PATH}"


# ----------------------------------------------------------------- trace shape


async def test_the_trace_summary_carries_conclusions_not_reasoning() -> None:
    """What lands in the workflow trace must satisfy Rule 18 too."""
    result = await _consult(_client_returning(_completed()))
    trace = result.as_trace()

    assert trace["status"] == "consulted"
    assert trace["amount_due"] == "500.00"
    assert trace["billing_eligible"] is True
    assert trace["rationale_summary"] == "Prorated for the remainder of the period."


async def test_the_trace_records_why_the_specialist_was_not_consulted() -> None:
    result = await _consult(_client_raising(httpx.ConnectError("refused")))
    trace = result.as_trace()

    assert trace["status"] == "unavailable"
    assert "ConnectError" in trace["detail"]
    assert "amount_due" not in trace


# --------------------------------------------------------------- corroboration


async def test_matching_figures_corroborate() -> None:
    result = await _consult(_client_returning(_completed()))
    agrees, divergence = corroborates(result, local_amount=Decimal("500.00"))

    assert agrees is True
    assert divergence is None


async def test_a_divergent_figure_is_reported_and_the_local_one_governs() -> None:
    """The specialist is corroboration, not an authority.

    An out-of-process agent that could change the amount charged would move the
    money decision outside the audited deterministic path.
    """
    result = await _consult(_client_returning(_completed()))
    agrees, divergence = corroborates(result, local_amount=Decimal("450.00"))

    assert agrees is False
    assert divergence is not None
    assert "500.00" in divergence
    assert "450.00" in divergence
    assert "using the local figure" in divergence


async def test_an_unavailable_specialist_neither_corroborates_nor_diverges() -> None:
    """Silence is not disagreement."""
    result = await _consult(_client_raising(httpx.ConnectError("refused")))
    agrees, divergence = corroborates(result, local_amount=Decimal("500.00"))

    assert agrees is False
    assert divergence is None
