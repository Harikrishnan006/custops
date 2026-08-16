"""Synthetic seed data.

Realistic, never real (BUILD_SPEC §1). Two properties make this more than
fixtures:

**Deterministic identity.** Every row's UUID is derived with ``uuid5`` from a
fixed namespace and a stable key, so seeding twice produces the same ids and
tests can reference a specific account without querying for it first. Re-running
the seed updates rather than duplicates.

**Failure paths are first-class.** BUILD_SPEC §5 requires enough variety to
exercise them, so the catalogue below deliberately contains an account that
cannot be upgraded, one that can only be upgraded with human approval, one whose
contract wording is ambiguous, and one drowning in support tickets. A seed set
where everything succeeds would let the whole system look correct while every
interesting branch went untested.

Dates are relative to ``now`` so the data is always current — a fixed calendar
would silently expire and start failing eligibility for the wrong reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.domain.enums import (
    AccountStatus,
    AuthorType,
    ContractStatus,
    CustomerStatus,
    EntitlementStatus,
    InvoiceStatus,
    SubscriptionStatus,
    TicketPriority,
    TicketStatus,
    UpgradeRestriction,
)
from custops.domain.models.billing import Discount, Invoice, Plan, Subscription
from custops.domain.models.contract import Contract, Policy
from custops.domain.models.customer import Account, Contact, Customer
from custops.domain.models.entitlement import Entitlement
from custops.domain.models.identity import Role, User, UserRole
from custops.domain.models.support import Conversation, SupportTicket

# Fixed namespace: changing this regenerates every id in the seed set.
SEED_NAMESPACE = uuid.UUID("6f0c9b3e-1f3a-5c7d-9e2b-4a8d6c0f1e35")


def seed_id(kind: str, key: str) -> uuid.UUID:
    """Derive a stable UUID for a seeded row."""
    return uuid.uuid5(SEED_NAMESPACE, f"{kind}:{key}")


# --- Plan catalogue --------------------------------------------------------

PLANS: tuple[dict[str, Any], ...] = (
    {
        "code": "starter",
        "name": "Starter",
        "rank": 1,
        "monthly_price": Decimal("49.00"),
        "annual_price": Decimal("490.00"),
        "features": {"seats_included": 5, "sso": False, "sla": "none"},
        "is_active": True,
    },
    {
        "code": "professional",
        "name": "Professional",
        "rank": 2,
        "monthly_price": Decimal("299.00"),
        "annual_price": Decimal("2990.00"),
        "features": {"seats_included": 25, "sso": True, "sla": "next_business_day"},
        "is_active": True,
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "rank": 3,
        "monthly_price": Decimal("999.00"),
        "annual_price": Decimal("9990.00"),
        "features": {"seats_included": 100, "sso": True, "sla": "4_hour"},
        "is_active": True,
    },
    {
        # Retired: exercises the TARGET_PLAN_INACTIVE blocker.
        "code": "professional_legacy",
        "name": "Professional (legacy)",
        "rank": 2,
        "monthly_price": Decimal("249.00"),
        "annual_price": Decimal("2490.00"),
        "features": {"seats_included": 20, "sso": True, "sla": "next_business_day"},
        "is_active": False,
    },
)


# --- Accounts, each exercising a different path ----------------------------

SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "key": "acme",
        "customer_name": "Acme Corporation",
        "external_ref": "ACME",
        "industry": "Manufacturing",
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "plan_code": "professional",
        "seats": 20,
        "contract": {"restriction": UpgradeRestriction.NONE, "status": ContractStatus.ACTIVE},
        "past_due_invoice": False,
        "discount_percent": None,
        "tickets": [("normal", TicketStatus.RESOLVED)],
        "note": "Happy path — upgrades cleanly with no approval.",
    },
    {
        "key": "globex",
        "customer_name": "Globex Industries",
        "external_ref": "GLOBEX",
        "industry": "Logistics",
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "plan_code": "professional",
        "seats": 40,
        "contract": {
            "restriction": UpgradeRestriction.TERM_LOCKED,
            "status": ContractStatus.ACTIVE,
        },
        "past_due_invoice": False,
        "discount_percent": None,
        "tickets": [],
        "note": "Contract locks the term — upgrade must be blocked.",
    },
    {
        "key": "initech",
        "customer_name": "Initech LLC",
        "external_ref": "INITECH",
        "industry": "Software",
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.SUSPENDED,
        "plan_code": "starter",
        "seats": 5,
        "contract": None,
        "past_due_invoice": True,
        "discount_percent": None,
        "tickets": [],
        "note": "Suspended account with a past-due invoice — two blockers at once.",
    },
    {
        "key": "umbrella",
        "customer_name": "Umbrella Health",
        "external_ref": "UMBRELLA",
        "industry": "Healthcare",
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "plan_code": "professional",
        "seats": 60,
        "contract": {"restriction": UpgradeRestriction.NONE, "status": ContractStatus.ACTIVE},
        "past_due_invoice": False,
        # Above the 20% default threshold — triggers approval.
        "discount_percent": Decimal("35.00"),
        "tickets": [("high", TicketStatus.PENDING)],
        "note": "Discount above threshold — eligible but requires approval.",
    },
    {
        "key": "vehement",
        "customer_name": "Vehement Capital",
        "external_ref": "VEHEMENT",
        "industry": "Financial Services",
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "plan_code": "professional",
        "seats": 15,
        "contract": {
            "restriction": UpgradeRestriction.AMBIGUOUS_TERMS,
            "status": ContractStatus.ACTIVE,
        },
        "past_due_invoice": False,
        "discount_percent": None,
        "tickets": [],
        "note": "Ambiguous contract wording — must escalate to a human.",
    },
    {
        "key": "hooli",
        "customer_name": "Hooli Systems",
        "external_ref": "HOOLI",
        "industry": "Technology",
        "customer_status": CustomerStatus.ACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "plan_code": "professional",
        "seats": 85,
        "contract": {"restriction": UpgradeRestriction.NONE, "status": ContractStatus.ACTIVE},
        "past_due_invoice": False,
        "discount_percent": None,
        "tickets": [
            ("urgent", TicketStatus.OPEN),
            ("urgent", TicketStatus.PENDING),
            ("high", TicketStatus.OPEN),
            ("normal", TicketStatus.RESOLVED),
            ("low", TicketStatus.CLOSED),
        ],
        "note": "Rich support history including open urgent tickets — warnings, not blockers.",
    },
    {
        "key": "soylent",
        "customer_name": "Soylent Foods",
        "external_ref": "SOYLENT",
        "industry": "Food & Beverage",
        "customer_status": CustomerStatus.INACTIVE,
        "account_status": AccountStatus.ACTIVE,
        "plan_code": "starter",
        "seats": 3,
        "contract": None,
        "past_due_invoice": False,
        "discount_percent": None,
        "tickets": [],
        "note": "Inactive customer — blocked at the relationship level.",
    },
)


CONTRACT_BODIES: dict[str, str] = {
    UpgradeRestriction.NONE: (
        "Section 4.2 — Plan Changes. The Customer may change subscription tier at "
        "any time during the Term. Changes take effect immediately and charges are "
        "prorated for the remainder of the current billing period."
    ),
    UpgradeRestriction.TERM_LOCKED: (
        "Section 4.2 — Plan Changes. The subscription tier is fixed for the "
        "committed Term. Any change to tier prior to the Term end date requires a "
        "written amendment executed by both parties. No tier change shall take "
        "effect absent such an amendment."
    ),
    UpgradeRestriction.REQUIRES_CUSTOMER_APPROVAL: (
        "Section 4.2 — Plan Changes. Tier changes require prior written "
        "authorisation from the Customer's designated procurement contact. "
        "Supplier shall not alter the subscription tier without such authorisation."
    ),
    UpgradeRestriction.AMBIGUOUS_TERMS: (
        "Section 4.2 — Plan Changes. The Customer may adjust the service level "
        "where operationally necessary, subject to the provisions of Schedule C "
        "and, where applicable, the notice requirements set out in Section 9.1, "
        "provided that such adjustment does not conflict with the commitments "
        "described elsewhere in this Agreement."
    ),
}


POLICIES: tuple[dict[str, Any], ...] = (
    {
        "code": "UPG-001",
        "title": "Subscription Upgrade Eligibility",
        "category": "subscription",
        "body": (
            "An account is eligible for a subscription upgrade when: the customer "
            "relationship is active; the account is active and not suspended; the "
            "subscription is in active status; there are no past-due invoices; and "
            "the governing contract does not restrict tier changes. Upgrades to a "
            "plan of equal or lower rank are not upgrades and must be processed as "
            "downgrades, which require approval."
        ),
    },
    {
        "code": "DIS-002",
        "title": "Discount Approval Thresholds",
        "category": "pricing",
        "body": (
            "Discounts of 20% or less may be applied by customer operations without "
            "escalation. Discounts above 20% require approval from a revenue "
            "operations approver before being applied. Discounts above 40% "
            "additionally require finance sign-off and a documented commercial "
            "justification recorded against the account."
        ),
    },
    {
        "code": "PRO-003",
        "title": "Proration on Mid-Cycle Plan Changes",
        "category": "pricing",
        "body": (
            "When a plan change takes effect part-way through a billing period, the "
            "customer is credited for the unused portion of the current plan and "
            "charged for the same portion at the new plan rate. Proration is "
            "calculated on whole days remaining in the period. Amounts are rounded "
            "to two decimal places, with halves rounded up."
        ),
    },
    {
        "code": "REF-004",
        "title": "Refund Authority",
        "category": "billing",
        "body": (
            "Refunds up to 1,000 USD may be issued by customer operations. Refunds "
            "above 1,000 USD require approval. Refunds are never issued to an "
            "account with an unsettled past-due balance without finance approval."
        ),
    },
    {
        "code": "ENT-005",
        "title": "Entitlement Provisioning and Verification",
        "category": "provisioning",
        "body": (
            "Entitlements are authoritative in the provisioning portal. A billing "
            "system plan change does not constitute provisioning. Every tier change "
            "must be verified by reading the entitlement back from the portal after "
            "the change. A billing record and an entitlement that disagree must be "
            "treated as a failed change and escalated, not reconciled silently."
        ),
    },
)


# Approvers, so the approval API has someone to authorise against (§13, §17).
# Three deliberately different actors: routine authority, elevated authority,
# and a deactivated account that must be refused.
SEED_ROLES: tuple[dict[str, str], ...] = (
    # Starting a workflow reaches billing, CRM and the legacy portal, so it is
    # its own role rather than something every approver happens to hold (§17).
    {"name": "operator", "description": "May start customer-operations workflows."},
    {"name": "approver", "description": "May approve routine operations."},
    {"name": "finance_approver", "description": "May approve high-value operations."},
    {"name": "viewer", "description": "Read-only; carries no approval authority."},
)

SEED_USERS: tuple[dict[str, Any], ...] = (
    {
        "key": "ops",
        "email": "ops.approver@custops.example.com",
        "full_name": "Ops Approver",
        "is_active": True,
        "roles": ("operator", "approver"),
    },
    {
        "key": "finance",
        "email": "finance.approver@custops.example.com",
        "full_name": "Finance Approver",
        "is_active": True,
        "roles": ("approver", "finance_approver"),
    },
    {
        # Holds no approving role — exercises the authority refusal path.
        "key": "viewer",
        "email": "viewer@custops.example.com",
        "full_name": "Read Only",
        "is_active": True,
        "roles": ("viewer",),
    },
    {
        # Deactivated — a role alone must not confer authority.
        "key": "former",
        "email": "former.approver@custops.example.com",
        "full_name": "Former Approver",
        "is_active": False,
        "roles": ("approver",),
    },
)


async def _seed_identity(session: AsyncSession, now: datetime) -> int:
    for role_data in SEED_ROLES:
        await session.merge(
            Role(
                id=seed_id("role", role_data["name"]),
                name=role_data["name"],
                description=role_data["description"],
            )
        )

    for user_data in SEED_USERS:
        await session.merge(
            User(
                id=seed_id("user", str(user_data["key"])),
                email=user_data["email"],
                full_name=user_data["full_name"],
                is_active=user_data["is_active"],
            )
        )
    await session.flush()

    for user_data in SEED_USERS:
        for role_name in user_data["roles"]:
            await session.merge(
                UserRole(
                    user_id=seed_id("user", str(user_data["key"])),
                    role_id=seed_id("role", role_name),
                    granted_at=now,
                )
            )
    await session.flush()
    return len(SEED_USERS)


async def seed_all(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Populate the database with the synthetic catalogue.

    Idempotent: rows are merged by their deterministic ids, so running it twice
    leaves the same data rather than a duplicated set. Does not commit — the
    caller owns the transaction.
    """
    reference = now if now is not None else datetime.now(UTC)

    counts = {
        "users": await _seed_identity(session, reference),
        "plans": await _seed_plans(session),
        "policies": await _seed_policies(session, reference),
        "customers": 0,
        "accounts": 0,
        "tickets": 0,
    }

    for scenario in SCENARIOS:
        await _seed_scenario(session, scenario, reference)
        counts["customers"] += 1
        counts["accounts"] += 1
        counts["tickets"] += len(scenario["tickets"])

    await session.flush()
    return counts


async def _seed_plans(session: AsyncSession) -> int:
    for plan_data in PLANS:
        await session.merge(
            Plan(
                id=seed_id("plan", str(plan_data["code"])),
                code=plan_data["code"],
                name=plan_data["name"],
                rank=plan_data["rank"],
                monthly_price=plan_data["monthly_price"],
                annual_price=plan_data["annual_price"],
                currency="USD",
                features=plan_data["features"],
                is_active=plan_data["is_active"],
            )
        )
    return len(PLANS)


async def _seed_policies(session: AsyncSession, now: datetime) -> int:
    for policy_data in POLICIES:
        await session.merge(
            Policy(
                id=seed_id("policy", str(policy_data["code"])),
                code=policy_data["code"],
                version=1,
                title=policy_data["title"],
                category=policy_data["category"],
                body=policy_data["body"],
                effective_from=now - timedelta(days=365),
                effective_to=None,
            )
        )
    return len(POLICIES)


async def _seed_scenario(session: AsyncSession, scenario: dict[str, Any], now: datetime) -> None:
    key = str(scenario["key"])

    customer_id = seed_id("customer", key)
    account_id = seed_id("account", key)

    await session.merge(
        Customer(
            id=customer_id,
            external_ref=scenario["external_ref"],
            name=scenario["customer_name"],
            industry=scenario["industry"],
            status=scenario["customer_status"],
        )
    )

    await session.merge(
        Account(
            id=account_id,
            customer_id=customer_id,
            name=f"{scenario['customer_name']} — Primary",
            status=scenario["account_status"],
            billing_email=f"billing@{key}.example.com",
            currency="USD",
            current_plan_code=scenario["plan_code"],
            lifecycle_stage="established",
            last_plan_change_at=now - timedelta(days=180),
        )
    )

    await session.merge(
        Contact(
            id=seed_id("contact", key),
            account_id=account_id,
            full_name=f"{scenario['customer_name'].split()[0]} Operations Lead",
            email=f"ops@{key}.example.com",
            role="Operations",
            is_primary=True,
        )
    )

    plan_id = seed_id("plan", str(scenario["plan_code"]))
    period_start = now - timedelta(days=10)
    period_end = period_start + timedelta(days=30)

    await session.merge(
        Subscription(
            id=seed_id("subscription", key),
            account_id=account_id,
            plan_id=plan_id,
            status=SubscriptionStatus.ACTIVE,
            billing_cycle="monthly",
            seats=scenario["seats"],
            started_at=now - timedelta(days=400),
            current_period_start=period_start,
            current_period_end=period_end,
            cancel_at_period_end=False,
        )
    )

    # The entitlement mirror starts in sync with billing. Phase 8's divergence
    # test is what deliberately breaks that.
    await session.merge(
        Entitlement(
            id=seed_id("entitlement", key),
            account_id=account_id,
            tier=scenario["plan_code"],
            seats=scenario["seats"],
            status=EntitlementStatus.PROVISIONED,
            last_synced_at=now - timedelta(hours=6),
        )
    )

    await _seed_invoices(session, key, account_id, scenario, now)

    if scenario["contract"] is not None:
        restriction = str(scenario["contract"]["restriction"])
        await session.merge(
            Contract(
                id=seed_id("contract", key),
                account_id=account_id,
                contract_number=f"CTR-{key.upper()}-001",
                status=scenario["contract"]["status"],
                starts_at=now - timedelta(days=400),
                ends_at=now + timedelta(days=330),
                auto_renew=True,
                minimum_term_months=24,
                notice_period_days=60,
                upgrade_restriction=restriction,
                price_increase_cap_percent=Decimal("7.00"),
                document_body=CONTRACT_BODIES[restriction],
            )
        )

    if scenario["discount_percent"] is not None:
        await session.merge(
            Discount(
                id=seed_id("discount", key),
                account_id=account_id,
                code=f"{key.upper()}-NEGOTIATED",
                percent_off=scenario["discount_percent"],
                reason="Negotiated at renewal",
                starts_at=now - timedelta(days=90),
                ends_at=now + timedelta(days=275),
                approved_by_user_id=None,
            )
        )

    await _seed_tickets(session, key, account_id, scenario, now)


async def _seed_invoices(
    session: AsyncSession,
    key: str,
    account_id: uuid.UUID,
    scenario: dict[str, Any],
    now: datetime,
) -> None:
    await session.merge(
        Invoice(
            id=seed_id("invoice", f"{key}-paid"),
            account_id=account_id,
            subscription_id=seed_id("subscription", key),
            number=f"INV-{key.upper()}-0001",
            status=InvoiceStatus.PAID,
            currency="USD",
            amount_due=Decimal("299.00"),
            amount_paid=Decimal("299.00"),
            issued_at=now - timedelta(days=40),
            due_at=now - timedelta(days=25),
            period_start=now - timedelta(days=40),
            period_end=now - timedelta(days=10),
            description="Monthly subscription",
        )
    )

    if scenario["past_due_invoice"]:
        await session.merge(
            Invoice(
                id=seed_id("invoice", f"{key}-overdue"),
                account_id=account_id,
                subscription_id=seed_id("subscription", key),
                number=f"INV-{key.upper()}-0002",
                status=InvoiceStatus.OPEN,
                currency="USD",
                amount_due=Decimal("49.00"),
                amount_paid=Decimal("0.00"),
                issued_at=now - timedelta(days=45),
                due_at=now - timedelta(days=15),
                period_start=now - timedelta(days=45),
                period_end=now - timedelta(days=15),
                description="Monthly subscription — unpaid",
            )
        )


async def _seed_tickets(
    session: AsyncSession,
    key: str,
    account_id: uuid.UUID,
    scenario: dict[str, Any],
    now: datetime,
) -> None:
    for index, (priority, status) in enumerate(scenario["tickets"]):
        ticket_id = seed_id("ticket", f"{key}-{index}")
        resolved = status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)

        await session.merge(
            SupportTicket(
                id=ticket_id,
                account_id=account_id,
                reference=f"TIC-{key.upper()}-{index + 1:03d}",
                subject=_TICKET_SUBJECTS[index % len(_TICKET_SUBJECTS)],
                category="technical" if priority in ("urgent", "high") else "billing",
                status=status,
                priority=TicketPriority(priority),
                opened_at=now - timedelta(days=30 - index),
                resolved_at=now - timedelta(days=25 - index) if resolved else None,
                satisfaction_score=4 if resolved else None,
            )
        )

        await session.merge(
            Conversation(
                id=seed_id("conversation", f"{key}-{index}"),
                ticket_id=ticket_id,
                author_type=AuthorType.CUSTOMER,
                author_name="Customer Contact",
                body=_TICKET_BODIES[index % len(_TICKET_BODIES)],
                created_at=now - timedelta(days=30 - index),
            )
        )


_TICKET_SUBJECTS = (
    "API rate limiting during bulk import",
    "SSO metadata refresh failing",
    "Invoice line items do not match usage report",
    "Webhook deliveries delayed",
    "Seat count not reflected after user removal",
)

_TICKET_BODIES = (
    "Our nightly import is hitting rate limits and failing partway through.",
    "The SSO metadata refresh has been failing since the certificate rotation.",
    "The usage report and the invoice disagree by roughly 12%.",
    "Webhook deliveries are arriving 20-30 minutes late.",
    "We removed three users but the seat count has not changed.",
)


async def clear_seed_data(session: AsyncSession) -> None:
    """Remove seeded customers and their cascade.

    Used by integration tests that need a known-empty starting point. Deletes by
    the deterministic ids only, so it cannot remove data it did not create.
    """
    for scenario in SCENARIOS:
        customer = await session.get(Customer, seed_id("customer", str(scenario["key"])))
        if customer is not None:
            await session.delete(customer)
    await session.flush()


async def seeded_plan_codes(session: AsyncSession) -> list[str]:
    """Plan codes currently present, for assertions and diagnostics."""
    return list((await session.execute(select(Plan.code).order_by(Plan.rank))).scalars())
