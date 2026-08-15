# ADR-005: HNSW over IVFFlat for the pgvector index

- **Status:** Accepted
- **Date:** 2026-08-15
- **Phase:** 3
- **Required by:** BUILD_SPEC §5 ("Choose the index type based on the current
  pgvector documentation and record the choice in an ADR")

## Context

pgvector offers two approximate-nearest-neighbour index types. Both trade exact
results for speed; they differ in how, and in what they demand of the data at
build time.

**IVFFlat** partitions vectors into lists around centroids computed by k-means,
then searches only the nearest few lists. The centroids are computed **when the
index is built**, from whatever data exists at that moment. Building it on an
empty or unrepresentative table produces bad centroids and permanently poor
recall until the index is rebuilt.

**HNSW** builds a multi-layer proximity graph incrementally as rows are
inserted. It needs no training data and no rebuild as the corpus grows.

## Decision

**HNSW**, over cosine distance, with `m = 16` and `ef_construction = 64`
(pgvector's documented defaults).

```sql
CREATE INDEX ix_knowledge_chunks_embedding_hnsw
ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

## Why

1. **The index is created before any data exists.** Migration 0004 runs on an
   empty database — exactly the condition under which IVFFlat produces
   meaningless centroids. Choosing IVFFlat would force either a build-after-seed
   step outside the migration (breaking "migrations bring the schema to a known
   state") or an index that silently under-performs until someone remembers to
   rebuild it.
2. **The corpus grows continuously.** Policies are versioned, contracts are
   added per account, and Phase 11's evaluation datasets add more. HNSW absorbs
   inserts without degrading; IVFFlat's recall drifts as the data moves away
   from the original centroids.
3. **Better recall/speed at this scale.** pgvector's documentation gives HNSW
   better query performance than IVFFlat at equivalent recall, at the cost of
   slower builds and more memory. At this corpus size (thousands of chunks),
   neither cost is material.
4. **Cosine, not L2.** Embedding vectors are normalised; cosine similarity is
   the metric the providers' models are trained against. The operator class
   (`vector_cosine_ops`) must match the query operator (`<=>`) — a mismatch is
   not an error, it just makes the planner ignore the index and fall back to a
   sequential scan, which shows up as latency rather than as a failure.

## Consequences

**Positive** — no build-order coupling between migration and seed; no rebuild as
the corpus grows; correct behaviour on an empty database.

**Negative** — slower index builds and higher memory use than IVFFlat. Neither
binds at this scale. If the corpus ever grows to where build time matters, that
is a measured re-decision with its own ADR, not a reason to pre-optimise now.

**Fixed dimensionality.** The column is `vector(1536)`, matching
`text-embedding-3-small`. Vectors of different widths are not comparable, so
switching to a wider model (`text-embedding-3-large` is 3072) is a migration
*and* a full re-embed of the corpus — not a config change.
`knowledge_chunks.embedding_model` records which model produced each vector, so
a half-migrated corpus is detectable rather than silently returning meaningless
distances.

## Alternatives considered

- **IVFFlat** — rejected above: needs representative data at build time, which
  a from-empty migration cannot provide.
- **No index (sequential scan)** — honest at this corpus size and would work,
  but it hides the index-choice question the spec explicitly asks to be
  answered, and the answer changes nothing else about the code.
- **A dedicated vector database** — out of scope; decision D4 settles it.
