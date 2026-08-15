"""The Billing Specialist's agent card.

Built with the SDK's own protobuf types (``a2a.types``, generated from
``a2a_pb2`` in a2a-sdk 1.x — not Pydantic, which an older memory would assume)
and serialised with ``MessageToDict``, so the published JSON is the
specification's shape rather than our approximation of it.

The card is what makes this discovery rather than configuration: a client is
pointed at a base URL and learns the capabilities, interfaces and modes from the
agent itself.
"""

from __future__ import annotations

from typing import Any

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from google.protobuf.json_format import MessageToDict

from custops.a2a.contracts.pricing import CAPABILITY_PRICING_DECISION

# The specification's well-known location. Clients look here without being told.
AGENT_CARD_PATH = "/.well-known/agent-card.json"

PROTOCOL_VERSION = "0.3.0"
AGENT_VERSION = "1.0.0"


def build_agent_card(base_url: str) -> AgentCard:
    """The Billing Specialist's published capabilities.

    One skill, narrowly described. A specialist advertising a broad surface
    would invite callers to treat it as a general billing service, and the
    boundary D6 is meant to represent — a finance-systems team owning one
    capability — would blur back into a shared library with a port number.
    """
    return AgentCard(
        name="billing-specialist",
        description=(
            "Reasons about subscription pricing. Given an account and a target "
            "plan, returns a structured pricing decision with rationale and "
            "confidence. Reads billing data through its own tool access; "
            "performs no mutations."
        ),
        version=AGENT_VERSION,
        provider=AgentProvider(
            organization="Customer Operations Platform",
            url=base_url,
        ),
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding="HTTP_JSON",
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=CAPABILITY_PRICING_DECISION,
                name="Pricing decision",
                description=(
                    "Price a proposed subscription plan change: proration, "
                    "eligibility, and whether human approval is required."
                ),
                tags=["billing", "pricing", "subscription"],
                examples=[
                    "Price an upgrade from professional to enterprise for account X.",
                ],
                input_modes=["application/json"],
                output_modes=["application/json"],
            )
        ],
    )


def card_to_json(card: AgentCard) -> dict[str, Any]:
    """Serialise the card as the specification's JSON.

    ``MessageToDict`` emits the camelCase names the spec uses
    (``supportedInterfaces``, ``defaultInputModes``), which a hand-written dict
    would eventually get wrong.
    """
    serialised: dict[str, Any] = MessageToDict(card)
    return serialised
