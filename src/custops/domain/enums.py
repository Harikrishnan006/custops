"""Domain vocabularies.

Every status and category in the business domain is declared here as a
``StrEnum``, then stored in a ``VARCHAR`` column rather than a PostgreSQL ``ENUM``
type — the same reasoning as ``observability.events``: extending a database enum
needs a migration and a lock, and the set of valid values is a business rule that
belongs where business rules live.

The values matter beyond storage: the deterministic rules in ``domain/rules``
compare against them, so a typo becomes a failing test rather than a silently
false eligibility decision.
"""

from __future__ import annotations

from enum import StrEnum


class CustomerStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CHURNED = "churned"


class AccountStatus(StrEnum):
    """An account can be suspended without the customer relationship ending."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class PlanTier(StrEnum):
    """Ordered by capability. ``rank`` on the Plan model carries the ordering;
    this enum only names the tiers a seeded catalogue uses."""

    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class BillingCycle(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class ContractStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"


class UpgradeRestriction(StrEnum):
    """Why a contract may block a mid-term plan change.

    This is the field that makes the Subscription Upgrade workflow interesting:
    an upgrade can be commercially sensible and contractually forbidden at the
    same time, and only a human can waive it.
    """

    NONE = "none"
    # Locked for the committed term; upgrading requires an amendment.
    TERM_LOCKED = "term_locked"
    # Requires written approval from the customer's procurement contact.
    REQUIRES_CUSTOMER_APPROVAL = "requires_customer_approval"
    # Wording is ambiguous — a human must interpret it (BUILD_SPEC §13).
    AMBIGUOUS_TERMS = "ambiguous_terms"


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class AuthorType(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"


class EntitlementStatus(StrEnum):
    PROVISIONED = "provisioned"
    PENDING = "pending"
    REVOKED = "revoked"
