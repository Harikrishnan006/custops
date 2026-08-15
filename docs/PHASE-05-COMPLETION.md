# Phase 5 — completion report

**Date:** 2026-08-15
**Scope:** LangGraph core — state, five agent nodes, conditional edges, retry/replan, checkpointing
**Status:** Code complete. Deterministic layer fully verified; database-backed node behaviour pending.

---

## What was built

**Across two commits.** `221d73b` delivered the skeleton (state, routing,
budgets, checkpointer, graph assembly); this one delivers the node
implementations wired to Phase 2 services, Phase 3 retrieval and Phase 4 MCP
tools.

| Piece | Where |
|---|---|
| `WorkflowState` with reducers | `agents/state.py` |
| Recovery budgets | `agents/budgets.py` |
| Conditional-edge routing | `agents/routing.py` |
| Cross-system validation (§14) | `agents/validation.py` |
| Structured-output schemas | `agents/schemas.py` |
| The ten nodes | `agents/nodes.py` |
| Graph assembly | `apps/orchestrator/graph.py` |
| Checkpointer | `apps/orchestrator/checkpointer.py` |

---

## API facts verified against installed packages, not recalled

LangGraph **1.2.11**, langgraph-checkpoint-postgres **3.1.2** — both majors past
the 0.2.x era. Confirmed by introspection:

- `interrupt(value)` lives in `langgraph.types`; resume via `Command(resume=…)`;
  an `Interrupt` carries `(value, id)`.
- `add_conditional_edges(source, path, path_map)`; `compile(checkpointer=…)`.
- The Postgres checkpointer speaks **psycopg 3**, not the asyncpg the rest of
  the application uses. `psycopg[binary]` ships prebuilt wheels — no compiler.
- `AsyncPostgresSaver.from_conn_string()` is an async context manager and
  `setup()` creates its own tables, deliberately outside our Alembic history.

This mattered: a recalled `FastMCP`-style import would have been wrong again.

---

## Two design changes forced by tests

**1. Budget bookkeeping moved into the graph.** Stub nodes that never
incremented `replan_count` produced an unbounded replan loop. That meant
enforcement depended on every node remembering to spend its own budget — the
exact failure budgets exist to prevent, reintroduced by the code meant to
prevent it. Recovery now routes through graph-owned `retry` and `replan` nodes
that `NodeSet` cannot substitute, so the loop shortens on every pass regardless
of node behaviour. Asserted by a test whose nodes increment nothing.

**2. Validation refuses to pass on two-of-three agreement.** See below.

---

## The divergence the architecture exists for, arriving on schedule

`execute` updates billing and the CRM. **Nothing flips the entitlement** — that
is Phase 8's Playwright step against the legacy portal (D8).

So a fully successful execution is *expected* to fail validation right now, and
does: billing PASS, CRM PASS, provisioning FAIL. The workflow reports its own
incompleteness rather than declaring success from two agreeing systems, which is
precisely the false confidence §14 is written to prevent.

`tests/integration/test_nodes.py::test_a_successful_execution_still_fails_validation`
asserts exactly this. It is not a known-failing test — it asserts the *correct*
current behaviour, and Phase 8 will change what it expects.

The divergence is recorded as **non-retryable**: retrying cannot make two
systems agree, only provisioning can.

---

## Verified (264 unit tests, ruff + mypy --strict clean over 119 files)

- **Routing** (34 tests): every §7 edge as a pure function of state — including
  that an unclassified request never falls through into the one workflow that
  exists, that high confidence cannot skip the approval gate, and that anything
  other than an explicit grant escalates.
- **Budgets**: retry-then-replan-then-escalate ordering, non-retryable failures
  skipping retry entirely, and a failure with no recorded error treated as
  non-retryable because silence is not evidence of a transient fault.
- **Graph topology** (13 tests): the assembled graph run end to end with
  recording stubs and an in-memory checkpointer — happy path, both escalation
  paths, the approval gate, both recovery loops, reducer accumulation.
- **Validation** (15 tests): all-agree, the billing-succeeded-entitlement-never-
  flipped divergence, two-of-three still failing, unreadable systems needing
  review rather than passing, `FAIL` outranking `NEEDS_REVIEW`, and a one-cent
  proration difference failing.
- **Checkpointer guard**: an in-memory checkpointer is refused outside
  `local`/`test`, because a run that cannot survive a restart is not a
  human-in-the-loop run.

## Not verified — pending infrastructure

**86 integration tests are written and skipped**, now including 12 new node
tests. Nothing has run against a database. Specifically unproven:

- Every node's database behaviour: evidence gathering, the deterministic
  assessment, tool-mediated execution, the cross-system read-back.
- **`interrupt()` and resume across a process restart** — the property that
  makes the approval gate real. `AsyncPostgresSaver` has never connected.
- The approval row written by `approval_gate` before it pauses.

The guard reports precisely why: `PostgreSQL unusable — localhost:5432 is
listening but unusable: InvalidPasswordError…`. Setup steps are in
[INTEGRATION-VERIFICATION.md](INTEGRATION-VERIFICATION.md).

---

## Decisions worth defending

**ADR-003 resolved.** Checkpoints live in PostgreSQL; a workflow paused for
approval is business state, not cache. Redis still holds nothing — Phase 4
rejected idempotency keys for it, Phase 3's content-addressed ingestion removed
the embedding-cache case. The criterion stands: a test must fail if Redis is
removed, or delete the dependency at Phase 13.

**Every node opens its own session.** A session cannot span an interrupt that
may last days.

**Reads go through MCP tools even from graph nodes.** The tool layer is the
audited, permission-checked boundary whether the caller is a model or a node —
and `test_execution_without_approval_is_refused_by_the_tool_layer` confirms D9
holds for nodes too.

**`send_notification` remains unimplemented.** `notify` drafts the message and
records `delivery: not_implemented`. A node reporting "notified" without sending
is exactly what the Validator exists to catch (Rule 6).

---

## Rule 23 — what you should be able to explain

1. Why routing functions are pure, and what that buys in testing.
2. Why budget increments belong to the graph rather than to the nodes.
3. Why a replan resets the retry count.
4. Why `NEEDS_REVIEW` escalates instead of retrying.
5. Why a failure with no recorded error is treated as non-retryable.
6. Why an unclassified request escalates rather than defaulting.
7. Why confidence can require approval but never waive it.
8. Why checkpoints are in PostgreSQL and what that means for Redis.
9. Why the checkpointer uses a different database driver, and why that is
   accepted.
10. Why `AsyncPostgresSaver`'s tables are not in our Alembic history.
11. Why every node opens its own session.
12. Why a successful execution currently fails validation, and why that is right.
13. Why the entitlement divergence is non-retryable.
14. Why `notify` records `not_implemented` instead of reporting success.
15. What is stored in a `Decision` and what is deliberately absent.
