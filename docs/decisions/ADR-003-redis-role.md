# ADR-003: What is Redis actually for?

- **Status:** ✅ **Accepted** — decided in Phase 5
- **Date raised:** 2026-08-15
- **Date decided:** 2026-08-15
- **Phase:** 1 (raised), 5 (decided)

## Decision

**Checkpoints live in PostgreSQL. Redis keeps no job yet, and is retained only
as the liveness dependency Phase 1's definition of done requires.**

`apps/orchestrator/checkpointer.py` uses LangGraph's `AsyncPostgresSaver`,
against the same database as everything else. The reasoning in the Context
section below held up once the checkpointer was actually wired: a workflow
paused at an approval gate is business state, and §7 requires it to survive a
process restart.

Two implementation facts emerged from wiring it, both worth recording because
neither was predictable from the spec:

1. **The checkpointer speaks psycopg 3, not asyncpg.** The rest of the
   application reaches PostgreSQL through SQLAlchemy + asyncpg. Two drivers
   against one database is a real cost, accepted because the alternative is
   writing a checkpointer by hand. `psycopg[binary]` ships prebuilt wheels, so
   this adds no build toolchain requirement.
2. **`AsyncPostgresSaver` owns its own tables** via internal migrations and
   `setup()`. They are deliberately absent from our Alembic history: they are
   the library's schema, versioned with the library, and duplicating them into
   our migrations would guarantee drift on the first upgrade.

## What this means for Redis

Redis still holds nothing. No code writes to it. Of the four candidate jobs
listed below, none has yet become real work:

- The **embedding cache** remains the strongest candidate, but Phase 3's
  ingestion is already content-addressed — unchanged documents are skipped
  without re-embedding — so the expensive repetition it would have prevented
  does not currently happen.
- **Tool idempotency keys** were considered and rejected in Phase 4 for the
  reason anticipated below: an idempotency record that can be evicted is an
  idempotency guarantee that can lapse. Phase 4 instead makes approvals
  single-use in PostgreSQL (`approvals.consumed_at`), which is durable.
- The **A2A response cache** is still weak and still Phase 9's to assess.

**The decision criterion stands: whatever Redis ends up doing must be
demonstrable by a test that fails if Redis is removed.** No such test exists
today. Option 4 — delete the dependency — is therefore live, and should be
taken at Phase 13 if nothing has claimed it by then. Retaining an idle service
because a spec listed it is exactly the answer §23 says would not survive
questioning.

---

## Original context (retained)

## Context

This ADR exists because two parts of BUILD_SPEC disagree, and the contradiction
should be recorded rather than quietly resolved in whichever direction the code
happened to go.

**Decision D2** cuts Celery and keeps Redis, justifying it as:

> Redis earns its place as the LangGraph checkpoint/cache backend alone.

**Section 7** specifies checkpointing as:

> LangGraph provides Postgres checkpointer packages and an interrupt mechanism
> for human-in-the-loop.

Both cannot be true. Checkpoints live in one store.

PostgreSQL is the right store for them:

- A workflow paused at an approval gate may wait hours or days. That state is
  business state, not cache — losing it loses an in-flight customer operation.
- BUILD_SPEC §5 makes PostgreSQL the source of truth, and §16 requires a
  reconstructable trace. Checkpoints in a separate store that can be flushed
  independently weaken both.
- §7 explicitly requires that a workflow paused for approval survives a process
  restart.

That leaves Redis without the job D2 assigned it. Under Rules 7 and 8 ("no
technology for resume keywords", "every dependency must have a technical
purpose"), a dependency with no purpose should be removed — and "we kept Redis
because the spec listed it" is exactly the answer that fails the §23 quality
standard under interview questioning.

## Status in Phase 1

Redis **is** in the stack: Phase 1's definition of done requires exactly three
services (`api`, `postgres`, `redis`) and a `/health` endpoint that
independently confirms Redis connectivity. That has been implemented, and this
ADR does not contradict it.

What is *not* claimed is that Redis currently does anything beyond being a
liveness dependency. It holds no data. No code writes to it.

## Candidate jobs (decide by Phase 5)

Each of these is real work Redis would do well; the decision is which, if any,
this system genuinely needs.

1. **Embedding cache (Phase 3).** Embedding the same policy text repeatedly
   costs money and latency. A content-hash-keyed cache is a legitimate Redis
   workload. Strongest candidate: the cost is measurable and the data is
   genuinely disposable.
2. **Tool idempotency keys (Phase 4).** A retried mutating MCP call must not
   double-apply. Note the tension: an idempotency record that can be evicted is
   an idempotency guarantee that can lapse, which argues for PostgreSQL.
3. **A2A response cache (Phase 9).** Pricing decisions for identical inputs.
   Weak: the inputs are rarely identical, and staleness has financial
   consequences.
4. **Nothing — remove it.** Legitimate outcome. Deleting a service is a
   defensible engineering result and a better interview answer than an idle
   dependency.

## Decision criteria

Choose in Phase 5, when the checkpointer is actually wired and its store is no
longer hypothetical. Whatever is chosen must be demonstrable: a test that fails
if Redis is removed. If no such test can be written, option 4 is the answer.
