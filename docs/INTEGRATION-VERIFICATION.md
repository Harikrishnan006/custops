# Integration verification — prerequisites and run plan

**Purpose.** Phases 1–4 are code-complete and unit-verified. Nothing has ever
touched a running database. This document is what you need to close that gap:
the exact state today, the exact setup steps for Windows, the run order, and an
honest list of what is most likely to break the first time.

**Status when this was written:** 6 commits, Phases 1–4 complete, Phase 5 not
started. `202 passed, 74 skipped`, ruff and `mypy --strict` clean across 104
files.

---

## 1. What is actually verified

| Layer | Verified | How |
|---|---|---|
| Deterministic business rules (pricing, eligibility, thresholds) | ✅ | 94 unit tests, no infrastructure |
| Chunking | ✅ | 21 unit tests: determinism, exact offsets, coverage, overlap, termination |
| Provider abstraction & capability boundaries | ✅ | Unit tests incl. Anthropic-has-no-embeddings |
| Evidence & retrieval-sufficiency rule | ✅ | Unit tests over scores |
| Permission matrix & tool error envelope | ✅ | Unit tests |
| Migrations match models (names only) | ✅ | Offline DDL rendered and diffed against `Base.metadata` |
| Config, logging, app lifespan | ✅ | Unit tests + a live uvicorn boot |
| `/health` **degraded** path | ✅ | Real uvicorn run with both dependencies down → 503 |
| MCP server assembly | ✅ | Builds; reports its 9 registered tools from the SDK registry |

## 2. What is NOT verified

**No migration has ever run. No row has ever been written. No vector has ever
been stored or retrieved. No approval has ever been checked against a real
record.**

The offline DDL test compares *table and column names* only. It does not check
types, server defaults, constraints, indexes, or foreign keys — `alembic check`
against a live server is the first thing that will.

### The 74 skipped integration tests

| File | Count | What it would prove |
|---|---:|---|
| `tests/integration/test_enterprise.py` | 29 | Seed data, CRM/billing/support/contract queries, and all seven upgrade-assessment branches end to end |
| `tests/integration/test_approval_enforcement.py` | 19 | **Decision D9** — direct tool call with no graph is refused; cross-entity, cross-execution, pending, rejected and replay cases; audit rows written on success *and* failure |
| `tests/integration/test_knowledge.py` | 16 | pgvector column, HNSW index, cosine ordering, account scoping, idempotent ingestion, citation offsets |
| `tests/integration/test_database.py` | 6 | Connectivity, pgvector extension present, schema at head |
| `tests/integration/test_health.py` | 4 | `/health` returning **200** — the success path has never happened |

These are skipped, not failing, and **must not be marked as passing** until they
actually run green.

---

## 3. Prerequisites — Windows

Already present on this machine: Python 3.11.9, `uv`, `git`. Docker and WSL2 are
**not** installed; the steps below avoid both.

### 3.1 PostgreSQL 17

```powershell
winget install PostgreSQL.PostgreSQL.17
```

Note the superuser password you set during install. Add the binaries to `PATH`
for the current session (or permanently via System Properties):

```powershell
$env:PATH += ";C:\Program Files\PostgreSQL\17\bin"
```

Verify:

```powershell
psql --version
```

### 3.2 pgvector — the fiddly part

pgvector has **no prebuilt Windows binary**. It must be compiled, which needs
the MSVC toolchain.

**Step 1 — Visual Studio Build Tools** (skip if you already have VS with C++):

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools"
```

**Step 2 — build and install pgvector.** This must run in the **x64 Native Tools
Command Prompt for VS 2022**, opened **as Administrator** (it writes into
`C:\Program Files`). This is `cmd`, not PowerShell:

```bat
set "PGROOT=C:\Program Files\PostgreSQL\17"
cd %TEMP%
git clone --branch v0.8.6 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

**Step 3 — confirm the server can see it** (back in PowerShell):

```powershell
psql -U postgres -c "SELECT name, default_version FROM pg_available_extensions WHERE name = 'vector';"
```

One row must come back. If it does not, migration `0001_enable_pgvector` will
fail and nothing downstream will run.

### 3.3 Database and role

```powershell
psql -U postgres -c "CREATE ROLE custops WITH LOGIN PASSWORD 'change-me-locally' SUPERUSER;"
```

```powershell
psql -U postgres -c "CREATE DATABASE custops OWNER custops;"
```

`SUPERUSER` is required because migration 0001 runs `CREATE EXTENSION`. Splitting
this into a privileged migration role and a least-privilege application role is
Phase 13 work — deliberately not done yet.

### 3.4 Redis

Redis has no official Windows build. Pick one (Memurai is the least friction
here, since Docker and WSL2 are both absent):

**Option A — Memurai** (Redis-compatible Windows service, free developer edition):

```powershell
winget install Memurai.MemuraiDeveloper
```

**Option B — WSL2** (heavier; also unlocks Docker later):

```powershell
wsl --install
```

then inside WSL: `sudo apt update && sudo apt install -y redis-server && sudo service redis-server start`

**Option C — Docker**, if you install Docker Desktop anyway:

```powershell
docker run -d -p 6379:6379 --name custops-redis redis:8.10
```

Verify something is listening:

```powershell
Test-NetConnection -ComputerName localhost -Port 6379 -InformationLevel Quiet
```

### 3.5 Application configuration

```powershell
Copy-Item .env.example .env
```

Edit `.env` so `POSTGRES_PASSWORD` matches the role you created. Defaults for
everything else are already correct for a local run:

- `POSTGRES_HOST=localhost`, `POSTGRES_USER=custops`, `POSTGRES_DB=custops`
- `REDIS_HOST=localhost`
- `CUSTOPS_ENVIRONMENT=local` — **required**, because the deterministic embedder
  is refused outside `local`/`test`
- `PROVIDER_EMBEDDING_DIMENSIONS=1536` — **must not change**; it is the width of
  the `vector(1536)` column created by migration 0004

No LLM API key is needed for any of this. Ingestion and retrieval run on the
deterministic embedder.

---

## 4. Run order

```powershell
uv sync
```

```powershell
uv run alembic upgrade head
```

```powershell
uv run alembic check
```

```powershell
uv run custops seed
```

```powershell
uv run custops ingest
```

```powershell
uv run pytest
```

**Expected on full success:** `276 passed` (202 unit + 74 integration), 0
skipped. Anything less means something below is real.

Optional sanity check of the live API:

```powershell
uv run uvicorn custops.apps.api.main:app --reload
```

then `curl http://localhost:8000/health` should return **200** with
`"pgvector_extension"` populated — the first time that path will ever have run.

---

## 5. Predicted failures, most likely first

These are honest predictions, not certainties. Migrations 0003–0005 are
hand-written and have never executed.

### 5.1 Known defect — a stale assertion that will fail

`tests/integration/test_database.py:23`

```python
HEAD_REVISION = "0002_foundation_tables"
```

The head is now `0005_approvals_and_tool_calls`. `test_schema_is_at_head_revision`
**will fail**. This is not a schema problem — it is a Phase 1 assertion that was
never updated as migrations 0003, 0004 and 0005 landed, and it stayed invisible
because the test has always skipped. It is a small, exact illustration of why
this verification pass matters.

Fix (deliberately not applied — left for the verification session):

```python
HEAD_REVISION = "0005_approvals_and_tool_calls"
```

Better still, derive it from Alembic's `ScriptDirectory` so it cannot go stale
again. `EXPECTED_TABLES` uses a subset check (`>=`) and will still pass.

### 5.2 `alembic check` reporting drift

The most valuable command in the list. The offline test only compares names, so
drift in **types, server defaults, constraints or indexes** would not have been
caught. Plausible spots: `Numeric(12, 2)` vs what SQLAlchemy renders, the
`Identity()` columns on `audit_events`/`tool_calls`, the `JSONB` server defaults,
and the `vector(1536)` column.

### 5.3 pgvector / HNSW

- `CREATE EXTENSION vector` fails → the extension was not installed into
  **this** PostgreSQL installation (§3.2 step 3 not verified).
- HNSW index creation fails → pgvector older than 0.5.0. The build above pins
  v0.8.6, so this should not happen.

### 5.4 Ingestion and retrieval

- `custops ingest` refusing with "meaningless similarity scores" →
  `CUSTOPS_ENVIRONMENT` is not `local` or `test`.
- Dimension mismatch on insert → `PROVIDER_EMBEDDING_DIMENSIONS` no longer 1536.
- `cosine_distance` ordering — the pgvector Python package is 0.5.0 and the
  server extension 0.8.6; they are independent versions and are expected to
  interoperate, but this is the first time they will meet.

### 5.5 Approval enforcement suite

The savepoint behaviour (`session.begin_nested()`) in
`mcp/tools/runtime.py` has never run against a real transaction. The 19 tests
in `test_approval_enforcement.py` are the first exercise of it — in particular
that a failed mutation rolls back while its audit rows still commit.

### 5.6 Seed and rollback isolation

Integration fixtures seed inside a transaction and roll back. If seeding is
slow or leaks between tests, the enterprise suite is where it will show.

---

## 6. Rules for the verification session

1. **Do not mark skipped tests as passing.** A skip is "not exercised"; only a
   green run changes that.
2. **Do not weaken a test to make it pass.** If an assertion is wrong, fix the
   assertion and say why. If the code is wrong, fix the code.
3. **Preserve all existing commits, tests and ADRs.**
4. Fix real integration failures **before** starting Phase 5.
5. Update the phase completion documents once items move from unverified to
   verified — those documents currently say "not verified", and that must stay
   accurate in both directions.

---

## 7. Resuming in a fresh session

Read, in order:

1. `docs/BUILD_SPEC.md` — the authoritative architecture and the 24 working rules
2. `docs/decisions/` — ADR-001 (layout), ADR-002 (single Postgres + pgvector),
   ADR-003 (**open**: what Redis is actually for, due Phase 5),
   ADR-005 (HNSW vs IVFFlat)
3. `docs/architecture/overview.md` — current state vs target
4. `docs/PHASE-01/02/03-COMPLETION.md` — what each phase verified and did not
5. This document

Then verify repository state before doing anything:

```powershell
git log --oneline
```

```powershell
uv run pytest -q
```

Expected before services exist: `202 passed, 74 skipped`. After a successful
verification pass: `276 passed`.

**Phase 5 (LangGraph) has not been started.** When it begins, two things must be
looked up in current documentation rather than recalled — the same way the MCP
SDK was, where a remembered `FastMCP` import would have been wrong:

- the LangGraph **Postgres checkpointer** package and class name
- the current **interrupt** API for human-in-the-loop

ADR-003 must also be resolved in Phase 5: decision D2 assigns Redis the
checkpoint store, §7 assigns it to PostgreSQL, and Redis currently holds nothing.
