# Phase 2 — completion report

**Date:** 2026-08-15
**Scope:** Domain models, deterministic business rules, enterprise service, seed data
**Status:** Code complete. Rules fully verified; database-backed paths unverified (no PostgreSQL).

---

## What was built

**Domain models (13 new tables).** CRM (`customers`, `accounts`, `contacts`),
billing (`plans`, `subscriptions`, `invoices`, `payments`, `discounts`),
contracts and policies, support (`support_tickets`, `conversations`), and
`entitlements` as the mirror of the legacy portal.

**Deterministic rules — the core of §12.** Pure functions over plain dataclasses,
no ORM objects, no session:

- `domain/rules/pricing.py` — proration, discounts, annualised value. All
  `Decimal`, rounding pinned to `ROUND_HALF_UP`.
- `domain/rules/eligibility.py` — upgrade eligibility, returning three distinct
  outcomes: blockers, approvals-required, warnings.
- `domain/policies/thresholds.py` — when a human must decide.

**Enterprise service.** CRM, billing, support and contracts as modules in one
service (D5), plus `assessment.py`, which assembles stored state into rule
inputs and returns a verdict with source-referenced evidence.

**Seed data.** Seven accounts, each driving a different branch: happy path,
term-locked contract, suspended account with arrears, deep discount, ambiguous
contract wording, heavy support load, inactive customer. Deterministic UUIDs, so
seeding is idempotent and tests reference known records.

---

## Verified

- **120 unit tests pass** (up from 36), `ruff` clean, `mypy --strict` clean
  across 70 files.
- **Pricing arithmetic**, including the property that matters: rounding is
  half-up, not banker's, and the invoice lines sum exactly to the reported total.
  Nonsensical inputs raise rather than returning a plausible number.
- **Eligibility**, every blocker/approval/warning path, including that all
  blockers are reported at once rather than one per round trip.
- **Approval thresholds**, including the load-bearing property: high model
  confidence can *never* remove an approval requirement, and an unrecognised
  action fails closed.
- **Migration matches the models.** `tests/unit/test_migration_schema_consistency.py`
  renders the whole migration chain as SQL in Alembic's offline mode, parses the
  DDL, and compares it column-by-column against `Base.metadata` — catching
  hand-written-migration drift with no database involved. All 17 tables and every
  column match.
- **Pricing adapter** (billing models → rule inputs), exercised with in-memory
  model instances.

## Not verified

1. **Nothing has touched a database.** 39 integration tests are written and
   skipped, including the full `test_enterprise.py` acceptance suite.
2. **`alembic check`** — the authoritative model/migration drift check —
   requires a server. The offline test above covers table and column names but
   not types, server defaults or constraints.
3. **Seed data has never been loaded.**
4. **The enterprise HTTP endpoints have never served a request.**

Closing the gap, once PostgreSQL exists:

```
uv run alembic upgrade head
uv run alembic check
uv run custops seed
uv run pytest
```

---

## Decisions worth defending

**`accounts.current_plan_code` is denormalised on purpose.** The plan is now
recorded in three places: `subscriptions.plan_id` (billing truth),
`entitlements.tier` (portal truth), `accounts.current_plan_code` (CRM's cached
copy). They can disagree, which is exactly the condition §14's cross-system
validation exists to detect. One field read three ways would make the Validator
theatre.

**Mutating operations exist but are not routed.** `apply_plan_change` and
`update_account_plan_reference` are service functions with no HTTP endpoint. All
enterprise routes are `GET`. A `PATCH /subscriptions/{id}` would be an unguarded
bypass of the three-layer approval architecture (D9); mutations reach these
functions only through MCP tools, which verify approval first.

**Rules take primitives, not ORM objects.** `UpgradeContext` is a dataclass of
plain values. The rule is therefore a pure function of stated inputs, testable
exhaustively without a database, and incapable of lazily loading something
nobody expected — and the Validator can recompute it from evidence alone.

**Contracts got their own module.** D5 names three domain modules; contracts sit
in a fourth because they belong to none of them. Filing them under billing would
make the eligibility rule look like a billing rule.

**A fourth plan (`professional_legacy`) is seeded inactive** purely so the
`TARGET_PLAN_INACTIVE` blocker has something to fire on.

---

## Rule 23 — what you should be able to explain

1. Why money is `Numeric`/`Decimal` and never `float`, and what specifically
   breaks in *this* system if it were float.
2. Why rounding is pinned to `ROUND_HALF_UP` rather than Python's default.
3. Why the two proration components are rounded before subtracting.
4. Why a downgrade returns a negative `amount_due` instead of zero.
5. Why `eligible` and `requires_approval` are separate properties.
6. Why the eligibility rule evaluates every condition instead of returning early.
7. Why rules take dataclasses of primitives rather than ORM objects.
8. Why `UNKNOWN_ACTION` requires approval, and what "fails closed" means here.
9. Why a model's confidence can only add approval requirements, never remove one.
10. Why the plan is stored in three places, and which one is authoritative for
    what.
11. Why `entitlements.last_synced_at` exists.
12. Why no enterprise endpoint mutates anything.
13. Why `plans.rank` exists rather than comparing tier names.
14. Why `count_past_due_invoices` computes from `due_at` and an injected `now`
    rather than trusting a stored status.
15. Why `assess_upgrade` takes `now` as a parameter.
16. Why an `AssessmentError` is a 404 while a blocked upgrade is a 200.
17. Why seed UUIDs are derived with `uuid5` rather than random.
18. How the migration/model consistency test works, and what it still cannot
    catch.
