"""SQLAlchemy models.

Importing this package must import every model module: ``Base.metadata`` is only
complete once each mapped class has been defined, and Alembic's autogenerate
compares the database against that metadata. A model that is not imported here is
invisible to migrations — which fails silently, in the worst possible direction.
"""

from __future__ import annotations

from custops.domain.models.audit import AuditEvent
from custops.domain.models.billing import Discount, Invoice, Payment, Plan, Subscription
from custops.domain.models.contract import Contract, Policy
from custops.domain.models.customer import Account, Contact, Customer
from custops.domain.models.entitlement import Entitlement
from custops.domain.models.identity import Role, User, UserRole
from custops.domain.models.support import Conversation, SupportTicket

__all__ = [
    "Account",
    "AuditEvent",
    "Contact",
    "Contract",
    "Conversation",
    "Customer",
    "Discount",
    "Entitlement",
    "Invoice",
    "Payment",
    "Plan",
    "Policy",
    "Role",
    "Subscription",
    "SupportTicket",
    "User",
    "UserRole",
]
