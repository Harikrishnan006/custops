# Phase 12 — completion report

**Date:** 2026-08-16
**Scope:** Observability — traces, audit, inspection endpoint (§16)
**Status:** Code complete. Taxonomy, redaction and trace assembly verified without
infrastructure; event persistence and the enriched endpoint pending PostgreSQL.

---

## What this phase actually found

§16 defines **19** structured event types. Before this phase the codebase emitted
**4**, from **3** hand-built `AuditEvent(...)` sites:

| | Emitted before | Emitted now |
|---|---|---|
| `tool_completed`, `approval_received`, `workflow_completed`, `workflow_failed` | ✅ | ✅ |
| The other **15** | ✗ dead enum members | ✅ |

The enum had carried all nineteen names since Phase 1, with a note in
`observability/events.py` saying *"Phase 12 owns the write path."* It did. A
taxonomy nobody emits is worse than none — it makes a trace look complete while
missing three quarters of what it claims to record.

---

## What was built

| Piece | Where |
|---|---|
| Single recording path | `observability/audit.py` |
| Centralised redaction | `observability/redaction.py` |
| Pure trace assembly | `observability/trace.py` |
| The 15 missing emissions | `agents/nodes.py`, `mcp/tools/runtime.py`, `apps/orchestrator/runner.py` |
| Enriched inspection endpoint | `apps/api/routers/workflows.py`, `apps/api/schemas/workflow.py` |

**No migration.** Verified before implementing: every §16 requirement maps onto an
existing column, and migration `0002` already creates the composite
`(execution_id, occurred_at)` index the trace query needs. Model and migration
agree — no drift.

---

## A latent ordering defect, found by verifying rather than assuming

`audit_events.occurred_at` defaults to `server_default=func.now()`. In PostgreSQL
`now()` is **transaction start time**, not wall clock — so every row written
inside one transaction carries an *identical* timestamp. The endpoint ordered by
`occurred_at` alone.

With 3 write sites this was invisible. Phase 12 takes a single tool call from one
event to three — `tool_selected` → `tool_called` → `tool_completed`, all in the
same savepoint — at which point the completion could sort **before** the call.

Fixed by ordering on `(occurred_at, id)`. `id` is a monotonic `BigInteger
Identity`, giving exact insertion order as a tie-break. Application-layer only;
no column, no index, no migration.

---

## Three design decisions worth recording

**`tool_called` is written outside the savepoint.** The handler runs inside
`begin_nested()`, so an audit row written beside it is rolled back when the
handler raises — losing precisely the fact that the tool *was* called before it
failed. A plain-Python `_Invocation` marker records that it ran; the row is
written afterwards, outside the savepoint, where it survives.

**`retry` / `replan` are emitted by the runner, not the nodes.** Those two nodes
are graph-owned and deliberately non-substitutable — the termination guarantee
depends on them running exactly as written — so handing them a session would give
them a failure mode they must not have. The runner already observes every node
visit, which makes it the honest place to record one.

**Redaction runs on the way in *and* on the way out.** The recorder redacts
before persisting; the endpoint redacts before serving. Duplicated deliberately:
rows may predate the recorder, and the endpoint is the boundary that actually
discloses.

---

## Verification

```
ruff check      clean
mypy --strict   clean, 164 files
pytest          490 passed, 156 skipped   (was 413 / 146 at 956aeb9)
```

**+77 passing, +10 pending.** No existing test was weakened, removed, or skipped.

| New suite | Passing | What it pins |
|---|---:|---|
| `test_event_taxonomy.py` | 25 | every event wired, and *where* |
| `test_redaction.py` | 21 | chain-of-thought dropped, secrets masked, bounds |
| `test_audit_recorder.py` | 17 | correlation, actor, no stray commit |
| `test_trace_assembly.py` | 14 | ordering, tie-break, coverage |

The taxonomy test is deliberately hard to bypass: it parses source with `ast`
(so a name in a comment cannot masquerade as an emission site), asserts the
enum matches §16 exactly in **both** directions, pins each event to the layer
that owns it, and fails if any module constructs `AuditEvent(...)` outside the
recorder. Adding a twentieth event without wiring it fails the build.

---

## Infrastructure-dependent verification (10 tests, pending)

All in `tests/integration/test_observability_trace.py`, skipped with the blocker
named: **PostgreSQL unusable — role `custops` does not exist
(`InvalidPasswordError`)**.

- Events persist and read back; redaction survives the round trip
- Rows written in one transaction genuinely share `occurred_at` — the premise
  behind the ordering rule, checked against real PostgreSQL rather than believed
- The monotonic `id` orders a shared timestamp correctly
- Ambient `execution_id` / `request_id` reach the columns
- An audit row survives naming an execution that does not exist
- The endpoint serves a merged, ordered timeline with event coverage
- The endpoint never serves chain-of-thought

Unchanged from earlier phases and still pending: full Subscription Upgrade traces
(**PostgreSQL**), the A2A pair against the real subprocess (**PostgreSQL** +
subprocess), provisioning events (**Chromium**).

---

## Constraints honoured

PostgreSQL/pgvector, MCP, A2A and Playwright boundaries unchanged. No Docker,
WSL2 or Build Tools. No deployment work. **AgentForge was not added as a
dependency** and none of its scoring, judging or regression logic is
reimplemented here — Phase 11 remains outstanding and now has a finished trace
structure to consume.

---

## Still outstanding

- **Phase 11** (§15): import `agent-forge@v0.1.0`, orchestrator evaluators,
  adversarial datasets, CI regression gating. Deferred by agreement so it could
  build on complete traces.
- Phases 13–14: security hardening; CI/CD and final documentation.
- 156 tests pending PostgreSQL / Chromium.
