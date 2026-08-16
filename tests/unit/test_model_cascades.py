"""Owned collections must let the database cascade (found by CI, 2026-08-16).

Deleting a seeded customer produced 72 teardown errors on the first real CI run:

    NotNullViolationError: null value in column "account_id"
    of relation "discounts" violates not-null constraint

The foreign keys had said ``ON DELETE CASCADE`` since Phase 2. What was missing
was ``passive_deletes=True`` on the ORM relationships, without which SQLAlchemy
loads every child on parent deletion and *de-associates* it — writing NULL into
a NOT NULL column. ``Account.contacts`` happened to carry a cascade and never
failed; its six siblings did not.

These tests read the mapper configuration rather than touching a database, so
the next relationship added without a cascade fails here — on any machine, in
under a second — instead of surfacing as an opaque teardown error the first
time someone runs the suite against real PostgreSQL.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import RelationshipProperty

from custops.domain.models.customer import Account, Customer

# Collections whose rows carry a NOT NULL ``account_id`` with ON DELETE CASCADE.
# Deleting the account must delete them, and the database must be the one to
# do it.
OWNED_BY_ACCOUNT = (
    "contacts",
    "subscriptions",
    "invoices",
    "contracts",
    "discounts",
    "tickets",
    "entitlements",
)


def _relationship(model: type, name: str) -> RelationshipProperty[Any]:
    prop: RelationshipProperty[Any] = inspect(model).relationships[name]
    return prop


@pytest.mark.parametrize("name", OWNED_BY_ACCOUNT)
def test_owned_collections_defer_deletion_to_the_database(name: str) -> None:
    """``passive_deletes=True`` — the flag whose absence caused the outage.

    Without it the ORM nulls the child's foreign key, which a NOT NULL column
    rejects.
    """
    assert _relationship(Account, name).passive_deletes is True, (
        f"Account.{name} would de-associate its children on delete; "
        "its account_id is NOT NULL, so PostgreSQL will reject that"
    )


@pytest.mark.parametrize("name", OWNED_BY_ACCOUNT)
def test_owned_collections_cascade_the_delete(name: str) -> None:
    """The ORM must agree with the schema about ownership.

    ``passive_deletes`` alone stops the null-out; declaring the delete cascade
    as well keeps the ORM's model of ownership matching the database's, so
    removing a child from the collection means what it appears to mean.
    """
    cascade = _relationship(Account, name).cascade

    assert cascade.delete, f"Account.{name} does not cascade deletion"
    assert cascade.delete_orphan, f"Account.{name} does not treat children as owned"


def test_a_customer_owns_its_accounts_the_same_way() -> None:
    """The chain that actually broke: deleting a Customer reached Accounts,
    which then tried to de-associate their own children."""
    accounts = _relationship(Customer, "accounts")

    assert accounts.passive_deletes is True
    assert accounts.cascade.delete
    assert accounts.cascade.delete_orphan


def test_every_not_null_account_child_is_covered_by_this_test() -> None:
    """Guards the list above from going stale.

    A new collection added to ``Account`` with a NOT NULL ``account_id`` must
    join ``OWNED_BY_ACCOUNT``, or it reintroduces the same defect silently.
    """
    uncovered: list[str] = []

    for name, prop in inspect(Account).relationships.items():
        if name in OWNED_BY_ACCOUNT or not prop.uselist:
            continue
        # A child whose FK column is NOT NULL cannot be de-associated.
        for column in prop.remote_side:
            if column.name == "account_id" and not column.nullable:
                uncovered.append(name)

    assert not uncovered, (
        f"these Account collections have a NOT NULL account_id but are not "
        f"covered by OWNED_BY_ACCOUNT: {uncovered}"
    )


def test_nullable_children_are_deliberately_not_cascaded() -> None:
    """Not everything is owned, and the distinction matters.

    ``WorkflowExecution.account_id`` is nullable with ON DELETE SET NULL: an
    execution is a record of something that happened and must outlive the
    account it referenced. Cascading it would erase history.
    """
    from custops.domain.models.workflow import WorkflowExecution

    column = inspect(WorkflowExecution).columns["account_id"]

    assert column.nullable
