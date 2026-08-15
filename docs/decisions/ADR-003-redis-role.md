# ADR-003: What is Redis actually for?

- **Status:** 🟡 **Proposed / open** — resolve in Phase 5
- **Date raised:** 2026-08-15
- **Phase:** 1 (raised), 5 (to be decided)

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
