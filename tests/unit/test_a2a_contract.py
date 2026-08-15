"""The A2A contract: the agent card and the capability payloads.

These are the parts another team codes against. A change here is a change to a
published interface, so the tests assert the *specification's* shape rather than
whatever the SDK happens to serialise today.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from custops.a2a.contracts.card import (
    AGENT_CARD_PATH,
    PROTOCOL_VERSION,
    build_agent_card,
    card_to_json,
)
from custops.a2a.contracts.pricing import (
    CAPABILITY_PRICING_DECISION,
    PricingDecisionRequest,
    PricingRecommendation,
)

BASE_URL = "http://127.0.0.1:8200"


def _card() -> dict[str, object]:
    return card_to_json(build_agent_card(BASE_URL))


def test_card_is_served_from_the_well_known_path() -> None:
    """The path is fixed by the spec; clients look there without being told."""
    assert AGENT_CARD_PATH == "/.well-known/agent-card.json"


def test_card_uses_the_specification_field_names() -> None:
    """camelCase, because that is what the wire format specifies.

    Hand-building this dict would eventually drift from the spec; the protobuf
    serialiser is what keeps the published names correct.
    """
    card = _card()
    assert card["name"] == "billing-specialist"
    assert "supportedInterfaces" in card
    assert "defaultInputModes" in card
    assert "defaultOutputModes" in card


def test_card_advertises_exactly_one_capability() -> None:
    """A narrow surface is the boundary.

    A specialist advertising a broad menu invites callers to treat it as a
    general billing service, and the separation D6 represents blurs back into a
    shared library with a port number.
    """
    skills = _card()["skills"]
    assert isinstance(skills, list)
    assert len(skills) == 1
    assert skills[0]["id"] == CAPABILITY_PRICING_DECISION


def test_card_points_callers_at_the_agents_own_url() -> None:
    interfaces = _card()["supportedInterfaces"]
    assert isinstance(interfaces, list)
    assert interfaces[0]["url"] == BASE_URL
    assert interfaces[0]["protocolVersion"] == PROTOCOL_VERSION


def test_card_declares_no_streaming() -> None:
    """The client is written for a synchronous exchange; the card must agree.

    A card claiming streaming would have callers waiting for events this agent
    never sends.
    """
    capabilities = _card().get("capabilities") or {}
    assert isinstance(capabilities, dict)
    assert capabilities.get("streaming") in (None, False)


# --------------------------------------------------------------- request shape


def test_request_carries_identifiers_not_billing_data() -> None:
    """The caller names the question; it does not supply the answer's inputs.

    This is what "owns its own tool access" means in practice: a caller cannot
    influence the specialist's figure by choosing what data to hand over.
    """
    fields = set(PricingDecisionRequest.model_fields)
    assert fields == {"account_id", "target_plan_code", "execution_id"}


def test_request_rejects_an_empty_target_plan() -> None:
    with pytest.raises(ValidationError):
        PricingDecisionRequest(account_id=uuid.uuid4(), target_plan_code="")


def test_execution_id_is_optional_so_the_agent_can_be_called_standalone() -> None:
    """The specialist is independently useful, not only inside a workflow."""
    request = PricingDecisionRequest(account_id=uuid.uuid4(), target_plan_code="enterprise")
    assert request.execution_id is None


# -------------------------------------------------------------- response shape


def _recommendation(**overrides: object) -> PricingRecommendation:
    payload: dict[str, object] = {
        "account_id": uuid.uuid4(),
        "current_plan_code": "professional",
        "target_plan_code": "enterprise",
        "amount_due": Decimal("500.00"),
        "currency": "USD",
        "unused_credit": Decimal("250.00"),
        "new_plan_charge": Decimal("750.00"),
        "days_remaining": 15,
        "days_in_period": 30,
        "billing_eligible": True,
        "approval_indicated": False,
        "confidence": 0.9,
        "rationale_summary": "Prorated for the remainder of the period.",
    }
    payload.update(overrides)
    return PricingRecommendation.model_validate(payload)


def test_verdict_fields_are_named_for_the_scope_the_specialist_can_see() -> None:
    """``billing_eligible``, not ``eligible``.

    The specialist's role cannot read customer or account standing and no tool
    exposes negotiated discounts. Unqualified names would invite the
    orchestrator to substitute this partial view for its own complete one —
    which is how a churned customer gets upgraded.
    """
    fields = set(PricingRecommendation.model_fields)
    assert "billing_eligible" in fields
    assert "approval_indicated" in fields
    assert "eligible" not in fields
    assert "requires_approval" not in fields


def test_confidence_is_bounded() -> None:
    """An out-of-range confidence would silently distort any policy reading it."""
    with pytest.raises(ValidationError):
        _recommendation(confidence=1.5)
    with pytest.raises(ValidationError):
        _recommendation(confidence=-0.1)


def test_rationale_summary_is_length_capped() -> None:
    """A summary, not a transcript.

    The cap is a structural guard on Rule 18: a field that can hold a thousand
    words will eventually hold reasoning, and this one crosses a process
    boundary into the workflow trace.
    """
    with pytest.raises(ValidationError):
        _recommendation(rationale_summary="x" * 501)


def test_amounts_survive_a_json_round_trip_without_float_drift() -> None:
    """Money is Decimal on both sides of the boundary.

    A recommendation is serialised to JSON, crosses a process boundary, and is
    revalidated. If that trip went through float, the specialist's figure would
    stop matching the local one for reasons that have nothing to do with data.
    """
    original = _recommendation(amount_due=Decimal("1234.57"))
    revalidated = PricingRecommendation.model_validate(original.model_dump(mode="json"))
    assert revalidated.amount_due == Decimal("1234.57")
    assert revalidated.amount_due == original.amount_due
