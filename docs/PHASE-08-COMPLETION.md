# Phase 8 — completion report

**Date:** 2026-08-15
**Scope:** Legacy portal + Playwright execution + cross-system validation
**Status:** Code complete. Boundary and rules verified; browser and database paths pending.

---

## What was built

| Piece | Where |
|---|---|
| The legacy provisioning portal | `apps/legacy_portal/app.py` |
| Provisioning boundary + stub | `provisioning/client.py` |
| Playwright driver | `provisioning/playwright_client.py` |
| `update_entitlement`, `get_entitlement` tools | `mcp/` matrix, schemas, handlers |
| Provisioning step + portal-backed validation | `agents/nodes.py` |
| Portal configuration | `config.py` |

The chain the phase existed to close now runs end to end:
**Billing → CRM → Legacy Portal → cross-system validation.**

---

## A defect found and fixed: approvals authorised nothing

While wiring provisioning I checked what the approval gate creates against what
execute verifies:

- Gate created `(subscription_upgrade, **account**, account_id)`
- `update_subscription` verified `(subscription_upgrade, **subscription**, …)`
- `update_crm` verified `(**update_crm**, account, …)`

**Neither mutation was authorised by the approval the gate created.** An approved
workflow failed at execute with `approval_required`. Phase 7's tests did not
catch it because they assert the workflow *resumed*, not that it succeeded.

Fixed by making the model match how a human actually decides: **one approval per
workflow, verified by every mutating tool, consumed exactly once.** A person
approves *the upgrade*, not three technical steps. `execute_tool` gained
`approval_action` and `consume_approval`; billing and CRM verify without
consuming, and provisioning — the last mutation — spends it. Consuming on the
first would have left billing changed and provisioning refused, manufacturing
the very divergence §14 exists to catch.

D9 is unweakened: **every** mutating tool still verifies independently, including
the browser step.

---

## Design decisions

**The portal has no API, and is tested for it.** `openapi_url=None`, no docs, no
JSON route. `test_openapi_is_not_served` exists because an API appearing later
would quietly remove the reason Playwright is in this project at all.

**Order: billing → CRM → portal.** Provisioning is last because it is slowest and
least reversible. A browser failure after billing and CRM leaves a divergence the
Validator catches; a portal flip followed by a failed billing update leaves one
nothing points at.

**The Validator reads the portal, not our mirror.** `get_entitlement` drives a
browser read of the portal's own page rather than querying `entitlements`.
Querying the table would check our side of the integration, and a validator that
validates itself proves nothing.

**The confirmation is read back, never echoed.** `set_tier` re-reads the rendered
tier after submitting and reports what the portal *says*. `matches_request` makes
disagreement explicit — a form that submits successfully and provisions something
else is a real failure mode, and echoing the request would hide it.

**Selectors are ids, not text** (`#tier`, `#apply`, `#current-tier`). Text
selectors are how browser suites become brittle, and a legacy portal whose copy
nobody controls is exactly where that bites.

**No provisioning client configured is a failure, not a skip.** An unprovisioned
upgrade that reports success is the outcome this architecture exists to prevent.

---

## Verified (338 unit tests, ruff + mypy --strict clean over 139 files)

33 new tests needing neither browser nor database:

- **The portal has no API**; login/logout, invalid credentials redirecting rather
  than status-coding (legacy behaviour the driver must detect by URL), an
  HttpOnly session cookie, forged cookies refused, and the tier-change POST
  gated — the mutation itself, not merely the page offering it.
- **The forced divergence** (§11's explicit ask): a portal confirming a different
  tier than requested is detected, and a later read returns the drifted value.
- **Every portal error maps to a tool code** — asserted exhaustively, so an
  unmapped code cannot silently become a generic upstream error. Timeouts are
  retryable; a missing account and a rejected tier are not.

## Not verified — pending infrastructure

**129 integration/e2e tests pending, 10 new.** Two gates now, reported separately:
PostgreSQL, and Chromium (`uv run playwright install chromium` — the package is
installed, the browser binary is a separate download).

Unproven: that the real form submits, that the driver's selectors match the
rendered page, that login carries a cookie through a real HTTP redirect, and that
the full Billing → CRM → Portal → validation chain agrees.

The matched pair that will prove it:

- `test_a_fully_provisioned_execution_passes_validation` — the whole chain
  agreeing.
- `test_a_portal_that_provisions_the_wrong_tier_is_caught` — every step reporting
  success while the portal provisioned something else, and validation failing
  anyway.

A system that only ever agrees with itself has not been shown to detect anything,
which is why both exist.

---

## Rule 23 — what you should be able to explain

1. Why the portal has no API, and what would be lost by adding one.
2. Why the approval model changed, and what was broken before.
3. Why billing and CRM verify without consuming, and provisioning consumes.
4. Why provisioning is the last mutation.
5. Why the Validator reads the portal instead of the entitlements table.
6. Why `set_tier` reads the tier back rather than echoing the request.
7. Why selectors are ids rather than text.
8. Why a missing provisioning client fails rather than skips.
9. Which portal errors are retryable, and why a rejected tier is not.
10. What the stub client proves, and what it cannot.
