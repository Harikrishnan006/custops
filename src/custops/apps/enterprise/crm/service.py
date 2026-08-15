"""CRM operations.

Every function takes an ``AsyncSession`` and returns domain objects or ``None``.
None of them commit: transaction boundaries belong to the caller, because a
workflow step that updates billing *and* the CRM must be able to do both in one
transaction or neither.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from custops.domain.models.customer import Account, Contact, Customer


async def get_customer_by_ref(session: AsyncSession, external_ref: str) -> Customer | None:
    """Look a customer up by the handle a human would use ("ACME").

    Case-insensitive: a request says "upgrade Acme", and requiring the caller to
    know the stored casing would push a data-entry detail into the workflow.
    """
    statement = (
        select(Customer)
        .where(Customer.external_ref.ilike(external_ref))
        .options(selectinload(Customer.accounts))
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def search_customers_by_name(
    session: AsyncSession, name_fragment: str, limit: int = 10
) -> list[Customer]:
    """Find candidate customers by partial name.

    Returns candidates rather than a best guess. Resolving ambiguity between
    "Acme Corp" and "Acme Industries" is a decision with consequences; the
    Supervisor escalates it rather than picking.
    """
    statement = (
        select(Customer)
        .where(Customer.name.ilike(f"%{name_fragment}%"))
        .order_by(Customer.name)
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars())


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> Account | None:
    statement = (
        select(Account)
        .where(Account.id == account_id)
        .options(selectinload(Account.customer), selectinload(Account.contacts))
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def list_accounts_for_customer(
    session: AsyncSession, customer_id: uuid.UUID
) -> list[Account]:
    statement = select(Account).where(Account.customer_id == customer_id).order_by(Account.name)
    return list((await session.execute(statement)).scalars())


async def get_primary_contact(session: AsyncSession, account_id: uuid.UUID) -> Contact | None:
    """The person a confirmation notification is addressed to."""
    statement = (
        select(Contact)
        .where(Contact.account_id == account_id, Contact.is_primary.is_(True))
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def update_account_plan_reference(
    session: AsyncSession,
    account_id: uuid.UUID,
    plan_code: str,
    changed_at: datetime,
) -> Account | None:
    """Sync the CRM's cached plan for an account.

    **Mutating — not exposed over HTTP.** Reachable only through the MCP
    ``update_crm`` tool, which verifies an approval record first (decision D9).

    Does not commit. The caller owns the transaction so that the CRM update and
    the billing update either both land or neither does.
    """
    account = await session.get(Account, account_id)
    if account is None:
        return None

    account.current_plan_code = plan_code
    account.last_plan_change_at = changed_at
    return account
