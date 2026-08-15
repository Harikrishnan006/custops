# Phase 3 — completion report

**Date:** 2026-08-15
**Scope:** Knowledge & RAG — provider abstraction, ingestion, chunking, embeddings, pgvector retrieval, Evidence
**Status:** Code complete. Unit-verified; database-backed paths unverified (no PostgreSQL).

---

## What was built

**Provider abstraction (D11, pulled forward from Phase 5 because Phase 3 needs
embeddings).** Capabilities are separate Protocols, a registry resolves provider
choice from config, and no business logic names a vendor. OpenAI and Google
embedding adapters; Anthropic declared chat-only.

**Knowledge schema.** `knowledge_documents` (the thing a human cites) and
`knowledge_chunks` (the unit that gets embedded), with a `vector(1536)` column
and an HNSW index over cosine distance.

**Ingestion.** Deterministic character-based chunking with exact source offsets;
content-addressed, idempotent ingestion of policies and contracts;
`custops ingest` CLI command.

**Retrieval and Evidence.** Cosine-distance search with account scoping, and an
`Evidence` model carrying source-referenced, span-precise items — never prose.

---

## The finding worth keeping: Anthropic has no embeddings endpoint

Its API surface is Messages, Batches, Files and Token Counting. There is no
`/v1/embeddings`.

Three options existed. Implement Anthropic's `embed()` by calling another vendor
behind Anthropic's name — a lie in the trace and in the audit log. Return zeros
— a silent corruption of every similarity score. Or model capabilities
separately and fail loudly. The third is what this codebase does: selecting
Anthropic for embeddings raises `CapabilityNotSupportedError` **at configuration
time**, not mid-workflow, because a system that discovers its embedder is
unusable halfway through retrieval has already burned a turn and half a trace.

This is why `providers/` has one Protocol per capability rather than one
uniform `Provider` interface. It looked like over-engineering until the API
surface disagreed with the assumption.

---

## Verified (178 unit tests, ruff + mypy --strict clean)

- **Chunking** (21 tests): determinism, exact offsets (`source[start:end] ==
  chunk.text`), full coverage with no gaps, real overlap between neighbours,
  boundary preference (paragraph → sentence → word), and termination on
  unbreakable input.
- **The deterministic embedder**: stable across runs, unit-length vectors,
  order preserved across a batch, honours configured dimensionality.
- **Capability boundaries**: Anthropic raises for embeddings; the registry
  refuses the deterministic stand-in outside `local`/`test`; missing API keys
  fail at construction.
- **OpenAI adapter ordering**: a deliberately out-of-order API response is
  re-sorted by index — a mis-ordered batch would attach every chunk to the
  wrong text with no visible error.
- **Sufficiency rule**: the escalate-vs-decide branch (§7) as a pure function of
  similarity scores — empty, all-below-floor, strong-single-match, and
  minimum-results paths.
- **Evidence**: citations carry the character span; the audit payload contains
  citations and scores but **not** content or narration (Rule 18).
- **Migration/model consistency**: both new tables and every column matched
  automatically by the existing offline-DDL test.

## Not verified

16 new integration tests are written and skipped. Nothing has embedded, stored,
or retrieved a vector — the pgvector column, the HNSW index, cosine ordering and
account scoping are all unexercised. Neither is a real embedding provider: no
API key is configured, so OpenAI and Google adapters have never made a call.

Closing the gap:

```
uv run alembic upgrade head
uv run custops seed
uv run custops ingest
uv run pytest -m integration
```

---

## Decisions worth defending

**HNSW, not IVFFlat** ([ADR-005](decisions/ADR-005-pgvector-index-type.md)). The
index is created by a migration on an empty database — exactly the condition
under which IVFFlat computes meaningless centroids.

**Character-based chunking, not token-based.** Token counts are tokenizer-
specific, so a token-sized chunker silently re-chunks the entire corpus when the
embedding model changes.

**Chunks store their source offsets.** An approval request can quote the span
that supports a claim rather than paraphrasing it.

**Account scoping is a correctness boundary.** Search without account context
returns global documents only — never "all documents", which would retrieve one
customer's contract while reasoning about another.

**Ingestion is content-addressed.** Re-ingesting unchanged text is a no-op;
changed text replaces that document's chunks wholesale. Without this, every run
would grow the corpus and retrieval would return three versions of one clause.

**The deterministic embedder is a labelled test double, not a feature.** It
makes the entire pipeline testable with no API key and no network, and the
registry refuses it outside `local`/`test`.

---

## Rule 23 — what you should be able to explain

1. Why capabilities are separate Protocols instead of one `Provider` interface.
2. Why selecting Anthropic for embeddings fails at configuration time rather
   than at first use.
3. Why the deterministic embedder is not a Rule 6 violation, and what stops it
   reaching production.
4. Why chunking is character-based rather than token-based.
5. What `source[start:end] == chunk.text` buys, and who consumes it.
6. Why chunks overlap, and what breaks at zero overlap.
7. Why HNSW rather than IVFFlat, in terms of *when the index is built*.
8. Why the query must use the operator the index was built for, and what the
   symptom is when it doesn't (hint: not an error).
9. Why changing the embedding model is a migration.
10. Why `knowledge_chunks.embedding_model` exists.
11. Why search without an account returns global documents only.
12. Why sufficiency is computed from scores alone and never from content.
13. Why the audit payload carries citations but not chunk text.
14. Why ingestion hashes the body, and what happens on re-ingest.
15. Why the OpenAI adapter sorts the response by index.
