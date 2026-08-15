# Phase 6 — completion report

**Date:** 2026-08-15
**Scope:** Subscription Upgrade end-to-end, API path only
**Status:** Code complete. Deterministic layer verified; the end-to-end path itself is pending infrastructure.

---

## What was built

| Piece | Where |
|---|---|
| `workflow_executions`, `workflow_steps` | `domain/models/workflow.py`, migration 0006 |
| Run orchestration and persistence | `apps/orchestrator/runner.py` |
| `POST /workflows`, `GET /workflows/{id}`, `GET /workflows` | `apps/api/routers/workflows.py` |
| Transport schemas | `apps/api/schemas/workflow.py` |

**API path only**, as §20 specifies: no browser automation (Phase 8), no A2A
specialist (Phase 9).

---

## Design decisions

**Runs inline, in the request.** No queue, no worker — D2 cut Celery and nothing
here justifies bringing it back. This works precisely because the graph does not
block on humans: at the approval gate it *interrupts* and returns, so a request
only ever waits for the automated portion of one run.

**Status codes carry meaning.** `201` for a run that finished, `202` for one
that paused awaiting a human. A client can tell the difference without parsing a
status string.

**Streaming, not final state.** `astream(stream_mode="updates")` yields each
node's update as it happens, so a step row is written *per visit*. A retry loop
that visits `execute` three times produces three rows; a final-state snapshot
would have hidden exactly what the budgets exist to bound.

**A paused run has no `finished_at`.** Stamping it would make an
awaiting-approval workflow indistinguishable from a completed one in every
report that filters on that column.

**Pausing is derived from the graph, not from state.** A node sets
`AWAITING_APPROVAL` before it interrupts, but the graph is the authority on
whether the run actually stopped. `_resolve_status` gives the interrupt
precedence.

**The trace joins three independently written records** — graph steps (runner),
tool calls (MCP layer), audit events (both) — by the one `execution_id` they all
carry. That they are written by different layers is what makes the trace worth
trusting.

**`agent_runs` from §5 is deliberately absent.** The provider layer does not
report token usage yet, so the table would be mostly nulls pretending to be
telemetry. It belongs to Phase 12 with the observability work that can populate
it.

**No approval-decision endpoint.** A run that pauses stays paused and reports
its prompt. Recording a human decision is Phase 7; inventing a thin version here
would put the approval record in two places. `WorkflowRunner.resume()` exists
and works — only the route is missing.

---

## Verified (280 unit tests, ruff + mypy --strict clean over 125 files)

The runner's persistence helpers are pure functions, so what gets written is
testable without a database:

- **Evidence is stored as citations, never content** — the text lives in the
  systems of record, and copying it in would duplicate data that can drift.
  Asserted by checking the retrieved prose is absent from the summary.
- **The summary shape is fixed**, so a new state key cannot silently start being
  persisted.
- **Decisions carry conclusions, not reasoning** (Rule 18) — the stored key set
  is asserted exactly.
- **A run with no status resolves to `failed`, not `completed`.** Absence of a
  status is not success.
- **UUIDs and datetimes are coerced** before hitting JSONB, so an insert cannot
  fail at commit far from its cause.
- Interrupt extraction, including a snapshot with no `interrupts` attribute.

## Not verified — pending infrastructure

**101 integration tests are written and skipped**, 15 of them new here. The
end-to-end path has never run. Specifically unproven:

- Any HTTP request against a real database.
- Whether the graph, the MCP tool layer and the runner actually compose —
  `astream` has never driven the real nodes.
- **The PostgreSQL checkpointer has never connected**, so interrupt-and-resume
  across a process restart remains untested.
- Trace reconstruction: that steps, tool calls and audit events all land under
  the same `execution_id` and can be joined.

The new tests assert the behaviour the earlier phases predicted, and will be the
first real check of it:

- `test_the_run_reaches_validation_and_reports_the_divergence` — the D8 failure
  surfaced through the API.
- `test_a_run_requiring_approval_pauses_with_202_and_a_prompt` — Umbrella's 35%
  discount tripping §13.
- `test_a_blocked_contract_escalates_without_executing` — Globex stopping before
  any mutation.

---

## Validation behaviour is unchanged, deliberately

`execute` updates billing and the CRM. Nothing flips the entitlement — that is
Phase 8's Playwright step — so a successful execution still fails validation:
billing PASS, CRM PASS, provisioning FAIL. The API returns that honestly rather
than reporting a completed upgrade.

This is asserted as correct current behaviour, not tolerated as a known failure.
Phase 8 will change what these tests expect.

---

## Rule 23 — what you should be able to explain

1. Why the run is synchronous, and why that does not block on humans.
2. Why `202` rather than `201` for a paused run.
3. Why steps are recorded per visit rather than per node.
4. Why a paused run has no `finished_at`.
5. Why pausing is read from the graph rather than from a node's status.
6. Why the trace joins three separately written records, and why that matters.
7. Why `final_state` stores citations rather than evidence text.
8. Why `agent_runs` was not created.
9. Why there is no approve endpoint yet.
10. Why a successful execution still fails validation, and why that is correct.
