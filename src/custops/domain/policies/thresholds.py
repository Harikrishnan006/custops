"""Approval thresholds — when a human must decide.

BUILD_SPEC §13 lists the triggers: large refunds, discounts above threshold,
destructive actions, ambiguous contract terms, low-confidence decisions, policy
exceptions. This module turns that list into a deterministic function.

Two properties make it trustworthy:

* **The model cannot influence it.** Confidence is an *input* the model
  supplies, and low confidence can only ever *add* an approval requirement.
  There is no path by which a model's output removes one.
* **It fails closed.** Unknown action types require approval rather than falling
  through to "allowed" — the default for an unrecognised high-risk operation
  must be to ask.

Enforcement is a separate concern: this decides *whether* approval is needed,
Phase 7 records the human decision, and the MCP tool layer independently
verifies the record before mutating anything (decision D9). This function being
correct is necessary and nowhere near sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class ActionType(StrEnum):
    """Actions the platform can be asked to perform."""

    SUBSCRIPTION_UPGRADE = "subscription_upgrade"
    SUBSCRIPTION_DOWNGRADE = "subscription_downgrade"
    SUBSCRIPTION_CANCEL = "subscription_cancel"
    APPLY_DISCOUNT = "apply_discount"
    ISSUE_REFUND = "issue_refund"
    UPDATE_CRM = "update_crm"
    SEND_NOTIFICATION = "send_notification"


class ApprovalTrigger(StrEnum):
    """Why approval is required."""

    DISCOUNT_ABOVE_THRESHOLD = "discount_above_threshold"
    REFUND_ABOVE_THRESHOLD = "refund_above_threshold"
    CHARGE_ABOVE_THRESHOLD = "charge_above_threshold"
    DESTRUCTIVE_ACTION = "destructive_action"
    CONTRACT_TERMS_AMBIGUOUS = "contract_terms_ambiguous"
    LOW_CONFIDENCE = "low_confidence"
    POLICY_EXCEPTION = "policy_exception"
    UNKNOWN_ACTION = "unknown_action"


# Actions that remove or reduce something a customer has. These always require a
# human, at any value: the cost of a wrongly-automated cancellation is not
# proportional to its dollar amount.
DESTRUCTIVE_ACTIONS = frozenset({ActionType.SUBSCRIPTION_CANCEL, ActionType.SUBSCRIPTION_DOWNGRADE})

# Actions this policy knows how to reason about. Anything else fails closed.
KNOWN_ACTIONS = frozenset(ActionType)


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Configured risk appetite.

    Defaults are deliberately conservative. Phase 7 wires these to settings so
    they can differ per environment; the shape is fixed now because the rules
    that consume it are being written now.
    """

    discount_percent_threshold: Decimal = Decimal("20.00")
    refund_amount_threshold: Decimal = Decimal("1000.00")
    charge_amount_threshold: Decimal = Decimal("10000.00")
    minimum_confidence: float = 0.70


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """The proposed action, described in terms the policy can evaluate."""

    action: str
    amount: Decimal | None = None
    discount_percent: Decimal | None = None
    confidence: float | None = None
    contract_terms_ambiguous: bool = False
    policy_exception: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Whether a human must sign off, and why."""

    required: bool
    triggers: tuple[ApprovalTrigger, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


def requires_approval(
    request: ApprovalRequest,
    policy: ApprovalPolicy | None = None,
) -> ApprovalDecision:
    """Decide whether ``request`` needs a recorded human approval.

    Every trigger is collected rather than returning on the first hit: an
    approval request should tell the reviewer everything that made it risky.
    """
    active_policy = policy if policy is not None else ApprovalPolicy()

    triggers: list[ApprovalTrigger] = []
    reasons: list[str] = []

    if request.action not in KNOWN_ACTIONS:
        # Fail closed. An action this policy has never heard of is not
        # implicitly safe.
        triggers.append(ApprovalTrigger.UNKNOWN_ACTION)
        reasons.append(f"Action '{request.action}' is not recognised by the approval policy.")

    if request.action in DESTRUCTIVE_ACTIONS:
        triggers.append(ApprovalTrigger.DESTRUCTIVE_ACTION)
        reasons.append(f"Action '{request.action}' reduces or removes customer entitlement.")

    if (
        request.discount_percent is not None
        and request.discount_percent > active_policy.discount_percent_threshold
    ):
        triggers.append(ApprovalTrigger.DISCOUNT_ABOVE_THRESHOLD)
        reasons.append(
            f"Discount of {request.discount_percent}% exceeds the "
            f"{active_policy.discount_percent_threshold}% threshold."
        )

    if request.amount is not None:
        if (
            request.action == ActionType.ISSUE_REFUND
            and request.amount > active_policy.refund_amount_threshold
        ):
            triggers.append(ApprovalTrigger.REFUND_ABOVE_THRESHOLD)
            reasons.append(
                f"Refund of {request.amount} exceeds the "
                f"{active_policy.refund_amount_threshold} threshold."
            )
        elif request.amount > active_policy.charge_amount_threshold:
            triggers.append(ApprovalTrigger.CHARGE_ABOVE_THRESHOLD)
            reasons.append(
                f"Amount {request.amount} exceeds the "
                f"{active_policy.charge_amount_threshold} threshold."
            )

    if request.contract_terms_ambiguous:
        triggers.append(ApprovalTrigger.CONTRACT_TERMS_AMBIGUOUS)
        reasons.append("Contract terms require human interpretation.")

    if request.confidence is not None and request.confidence < active_policy.minimum_confidence:
        triggers.append(ApprovalTrigger.LOW_CONFIDENCE)
        reasons.append(
            f"Decision confidence {request.confidence:.2f} is below the "
            f"{active_policy.minimum_confidence:.2f} minimum."
        )

    if request.policy_exception:
        triggers.append(ApprovalTrigger.POLICY_EXCEPTION)
        reasons.append("Action is a documented exception to standard policy.")

    return ApprovalDecision(
        required=bool(triggers),
        triggers=tuple(triggers),
        reasons=tuple(reasons),
    )
