"""The Billing Specialist's capability payloads.

A2A carries opaque message parts; what those parts *mean* is this contract. It
is typed on both sides — the specialist validates what it receives and the
client validates what comes back — so a protocol-level success carrying a
malformed body is a validation error rather than a plausible-looking wrong
answer.

**The request deliberately carries identifiers, not data.** It names an account
and a target plan; it does not pass the subscription, contract or pricing terms
along with it. That is what "owns its own tool access" means in practice (§9) —
the specialist reads what it needs through its own role, and a caller cannot
influence its answer by choosing what to hand over.

**The response is scoped to what a billing role can see**, and the field names
say so. The specialist's permissions cover subscription, plan, contract and
invoice data; they do not cover customer or account status, and there is no tool
exposing negotiated discounts. Its verdict is therefore a *billing* verdict.
Naming the fields ``eligible`` and ``requires_approval`` unqualified would invite
the orchestrator to substitute a partial view for its own complete one — which
is precisely how a churned customer gets upgraded.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

CAPABILITY_PRICING_DECISION = "billing.pricing_decision"


class PricingDecisionRequest(BaseModel):
    """Ask the specialist to price a proposed plan change."""

    account_id: uuid.UUID
    target_plan_code: str = Field(min_length=1, max_length=32)
    # Propagated so the specialist's own tool calls land under the same trace
    # (§16). Agent-to-agent hops are exactly where a correlation id gets lost.
    execution_id: uuid.UUID | None = None


class PricingRecommendation(BaseModel):
    """The specialist's structured decision (§9).

    The amount is computed by the same deterministic rules the orchestrator uses
    — deliberately. The value here is not a different algorithm but an
    *independent recomputation from independently fetched state*: if the two
    sides disagree on the figure, they read different data, and that is worth
    knowing loudly.
    """

    account_id: uuid.UUID
    current_plan_code: str
    target_plan_code: str

    amount_due: Decimal
    currency: str
    unused_credit: Decimal
    new_plan_charge: Decimal
    days_remaining: int
    days_in_period: int

    # Scoped to billing-visible facts: subscription status, plan ranks, target
    # plan activity, contract terms, past-due invoices. It says nothing about
    # customer or account standing, which this role cannot read.
    billing_eligible: bool
    billing_blockers: list[str] = Field(default_factory=list)

    # Likewise partial: derived from the amount and contract ambiguity, but not
    # from negotiated discounts, which no tool in this role exposes. A caller
    # may only ever use this to *raise* an approval requirement, never to lower
    # one — see the client's ``corroborates`` helper.
    approval_indicated: bool
    approval_triggers: list[str] = Field(default_factory=list)

    confidence: float = Field(ge=0.0, le=1.0)
    # A conclusion, not reasoning (Rule 18). Crosses a process boundary and
    # lands in the workflow trace, so the same rule applies here as anywhere.
    rationale_summary: str = Field(max_length=500)
    evidence_refs: list[str] = Field(default_factory=list)


class PricingDecisionError(BaseModel):
    """A refusal the specialist can express without failing the transport.

    The specialist was reachable and answered; the answer is "I cannot price
    this". Distinct from an unreachable specialist, which is not an answer at
    all, and which the caller handles by falling back rather than by reporting a
    blocker.
    """

    code: str
    message: str
