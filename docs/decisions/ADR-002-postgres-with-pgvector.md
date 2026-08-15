# ADR-002: One PostgreSQL service, with pgvector as an extension

- **Status:** Accepted
- **Date:** 2026-08-15
- **Phase:** 1
- **Implements:** BUILD_SPEC decision D4

## Context

The platform needs both relational state (customers, subscriptions, approvals,
audit) and vector similarity search over policy and contract documents. A common
reflex is to add a dedicated vector database alongside PostgreSQL.

pgvector is a PostgreSQL extension: `CREATE EXTENSION vector` adds a `vector`
column type and index support to an existing database.

## Decision

One PostgreSQL service. pgvector is enabled inside it by migration
`0001_enable_pgvector`, ahead of any table.

The extension is enabled by a *migration* rather than a container init script, so
that a native PostgreSQL install, a Docker Compose stack and CI all reach the
same schema state by running `alembic upgrade head` and nothing else.

Image: `pgvector/pgvector:0.8.6-pg17`, which ships the extension pre-built. A
native install must build the extension separately (see README).

## Consequences

**Positive**

- Retrieval joins directly against relational state — "the five policy chunks
  most similar to this query, restricted to contracts belonging to this
  customer" is one query, not two round trips and an application-side join.
- One backup, one transaction boundary, one connection pool, one failure domain.
  Evidence retrieved for a workflow is consistent with the state it is reasoning
  about.
- Two database services in a compose file, where one would do, reads as an
  architectural error to a reviewer.

**Negative**

- pgvector's index types are less specialised than a dedicated vector store's.
  At this system's data scale (synthetic seed data, thousands of chunks) this is
  not a real constraint. If it ever becomes one, that is a measured decision with
  its own ADR — not an assumption made in advance.
- Requires a PostgreSQL build that has the extension available, which is the
  main friction of a native Windows install.

## Deferred

The **index type** (HNSW vs IVFFlat) and its parameters are *not* decided here.
That choice depends on the corpus size and recall requirements, neither of which
exists until knowledge ingestion is built. BUILD_SPEC §5 requires the choice to
be recorded in an ADR; it will be made in Phase 3, against the current pgvector
documentation and measured recall, and recorded as ADR-005.

Phase 1 creates no vector column. Enabling the extension is a capability; using
it is Phase 3.
