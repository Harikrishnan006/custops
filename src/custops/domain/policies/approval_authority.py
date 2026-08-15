"""Who may decide an approval, and which approvals may be decided.

Layer 2 of §13's three-layer enforcement is "the approval API records the human
decision with actor and timestamp". These are the rules that layer applies, kept
as pure functions so every authorisation path is testable without a database, a
user, or an HTTP request.

Three distinct questions, deliberately separate:

* **Authority** — does this actor hold a role sufficient for this decision?
* **Decidability** — is this approval in a state where a decision is meaningful?
* **Freshness** — is a granted approval still safe to act on?

Collapsing them would produce one boolean that cannot explain itself. A human
refused an approval deserves to know whether they lacked the role, arrived after
someone else decided, or are looking at something already spent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class ApproverRole(StrEnum):
    """Roles that carry approval authority.

    Named here rather than in the identity model because authority is a policy
    question: which role may approve what is a business decision, while a role
    is merely a row.
    """

    APPROVER = "approver"
    FINANCE_APPROVER = "finance_approver"
    ADMIN = "admin"


class AuthorityDenial(StrEnum):
    """Why an actor may not decide."""

    ACTOR_NOT_FOUND = "actor_not_found"
    ACTOR_INACTIVE = "actor_inactive"
    NO_APPROVAL_ROLE = "no_approval_role"
    ELEVATED_ROLE_REQUIRED = "elevated_role_required"


class DecidabilityDenial(StrEnum):
    """Why an approval cannot be decided now."""

    ALREADY_DECIDED = "already_decided"
    ALREADY_CONSUMED = "already_consumed"
    NOT_PENDING = "not_pending"


@dataclass(frozen=True, slots=True)
class ApprovalAuthorityPolicy:
    """Configured approval authority.

    ``elevated_amount_threshold`` mirrors seeded policy DIS-002, which requires
    finance sign-off above a larger figure than routine approval. Encoding it
    here means the rule the documents describe and the rule the system enforces
    are the same rule.

    ``validity_window_hours`` bounds how long a granted approval stays usable.
    Without it, an approval granted against one state of the world could be
    spent weeks later against a different one — a replay in slow motion.
    """

    approver_roles: frozenset[str] = frozenset(
        {ApproverRole.APPROVER, ApproverRole.FINANCE_APPROVER, ApproverRole.ADMIN}
    )
    elevated_roles: frozenset[str] = frozenset({ApproverRole.FINANCE_APPROVER, ApproverRole.ADMIN})
    elevated_amount_threshold: Decimal = Decimal("10000.00")
    validity_window_hours: int = 24


@dataclass(frozen=True, slots=True)
class AuthorityCheck:
    permitted: bool
    denial: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class DecidabilityCheck:
    decidable: bool
    denial: str | None = None
    message: str = ""


def check_authority(
    *,
    actor_exists: bool,
    actor_is_active: bool,
    actor_roles: frozenset[str],
    amount: Decimal | None,
    policy: ApprovalAuthorityPolicy | None = None,
) -> AuthorityCheck:
    """Decide whether an actor may approve or reject.

    Fails closed at every step: an unknown actor, a deactivated one, or one
    holding no approval role is refused. Authority is never inferred from the
    fact that someone reached the endpoint.
    """
    active = policy if policy is not None else ApprovalAuthorityPolicy()

    if not actor_exists:
        return AuthorityCheck(False, AuthorityDenial.ACTOR_NOT_FOUND, "No such user.")
    if not actor_is_active:
        return AuthorityCheck(False, AuthorityDenial.ACTOR_INACTIVE, "User account is not active.")
    if not (actor_roles & active.approver_roles):
        return AuthorityCheck(
            False,
            AuthorityDenial.NO_APPROVAL_ROLE,
            "User holds no role carrying approval authority.",
        )

    # Larger amounts need the elevated role, matching policy DIS-002.
    needs_elevated = amount is not None and amount > active.elevated_amount_threshold
    if needs_elevated and not (actor_roles & active.elevated_roles):
        return AuthorityCheck(
            False,
            AuthorityDenial.ELEVATED_ROLE_REQUIRED,
            f"Amounts above {active.elevated_amount_threshold} require a finance approver.",
        )

    return AuthorityCheck(True)


def check_decidable(*, status: str, consumed_at: datetime | None) -> DecidabilityCheck:
    """Decide whether this approval may still receive a decision.

    An approval is decided exactly once. Re-deciding one is how a rejection
    becomes an approval after the fact, and how one human decision authorises
    work a different human refused.
    """
    if consumed_at is not None:
        return DecidabilityCheck(
            False,
            DecidabilityDenial.ALREADY_CONSUMED,
            f"Approval was already used at {consumed_at.isoformat()}.",
        )
    if status in ("approved", "rejected"):
        return DecidabilityCheck(
            False,
            DecidabilityDenial.ALREADY_DECIDED,
            f"Approval was already decided as '{status}'.",
        )
    if status != "pending":
        return DecidabilityCheck(
            False,
            DecidabilityDenial.NOT_PENDING,
            f"Approval has status '{status}' and is not awaiting a decision.",
        )
    return DecidabilityCheck(True)


def is_stale(
    *,
    decided_at: datetime | None,
    now: datetime,
    policy: ApprovalAuthorityPolicy | None = None,
) -> bool:
    """Whether a granted approval has aged out of usefulness.

    An approval with no decision timestamp is treated as stale: an approval that
    cannot say when it was granted cannot be shown to be current, and the safe
    reading of "unknown" is "no".
    """
    active = policy if policy is not None else ApprovalAuthorityPolicy()
    if decided_at is None:
        return True
    return now - decided_at > timedelta(hours=active.validity_window_hours)
