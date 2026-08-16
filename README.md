# AI Customer Operations Orchestrator

An enterprise agentic platform for B2B SaaS customer operations. It receives
natural-language business requests and converts them into executable, stateful,
auditable workflows that produce **real state changes across real systems** —
not text.

```
"Upgrade Acme from Professional to Enterprise. Check eligibility, verify the
 contract, calculate pricing, update the CRM and billing system, and send
 confirmation."
```

- **Architecture:** [docs/architecture/overview.md](docs/architecture/overview.md)
- **Full specification:** [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md)
- **Decisions:** [docs/decisions/](docs/decisions/)

> ### Current status: all 14 phases merged, `main` green
>
> **864 tests** — 703 unit, 154 integration, 7 end-to-end — across **6 CI jobs**
> (lint/types, unit, integration, end-to-end, packaging, image/compose) plus a
> separate evaluation-gate workflow. The integration job runs against a real
> `pgvector/pgvector:0.8.6-pg17` service container and Redis; the end-to-end job
> drives real Chromium.
>
> Per-phase reports, each stating what is verified and what is not:
> [01](docs/PHASE-01-COMPLETION.md) · [02](docs/PHASE-02-COMPLETION.md) ·
> [03](docs/PHASE-03-COMPLETION.md) · [04](docs/PHASE-04-COMPLETION.md) ·
> [05](docs/PHASE-05-COMPLETION.md) · [06](docs/PHASE-06-COMPLETION.md) ·
> [07](docs/PHASE-07-COMPLETION.md) · [08](docs/PHASE-08-COMPLETION.md) ·
> [09](docs/PHASE-09-COMPLETION.md) · [10](docs/PHASE-10-COMPLETION.md) ·
> [11](docs/PHASE-11-COMPLETION.md) · [12](docs/PHASE-12-COMPLETION.md) ·
> [13](docs/PHASE-13-COMPLETION.md) · [14](docs/PHASE-14-COMPLETION.md)

---

## What CustOps is

A request like the one above becomes a **stateful, resumable, audited workflow**
rather than a paragraph of generated text.

- **Orchestration — LangGraph.** A ten-node state machine: supervisor → planner →
  research → decide → approval gate → execute → validate → notify, with escalate
  and complete as terminal states. Routing is pure Python over workflow state; no
  model decides where the graph goes next.
- **Enterprise access — MCP.** Agents reach billing, the CRM, pricing, invoices,
  support history and the knowledge base only through Model Context Protocol
  tools, each bound to a role in a permission matrix. There is no path from an
  agent to raw SQL.
- **Specialist consultation — A2A.** A billing specialist runs as a genuinely
  separate process, discovered by agent card and reached over a socket. It may
  read and advise; it can neither approve nor mutate.
- **Durability — PostgreSQL checkpointing.** A run interrupted at the approval
  gate is checkpointed, so it survives a process restart and can wait for a human
  indefinitely. Resuming continues the same execution rather than replaying it.
- **Evidence — pgvector retrieval.** Policies and contracts are chunked, embedded
  and searched with an HNSW index. A decision made on evidence that scored below
  the confidence threshold escalates instead of proceeding.
- **Legacy systems — Playwright.** One system of record has no API, so it is
  driven through a real browser — behind the same MCP permission boundary as
  every other tool.
- **Approval — enforced in three places.** The graph will not route past the gate,
  the API records the human decision, and the MCP tool independently re-verifies
  an approval record before it mutates anything. The check is deliberately not
  in the agent.
- **Validation — cross-system.** After execution the validator re-reads each
  system of record and compares against intent. Agreement between two systems is
  not success if the third cannot be confirmed: unreadable is `NEEDS_REVIEW`, and
  disagreement escalates.

Architecture in depth: [docs/architecture/overview.md](docs/architecture/overview.md).
Contested decisions: [docs/decisions/](docs/decisions/) (7 ADRs).

---

## What is and isn't wired

Stated plainly, because the difference matters when reading the code or the CI
output.

| Area | Status |
|---|---|
| Model provider | **No live LLM adapter.** Every run uses `DeterministicChatProvider`, a fixed schema-valid responder. No Anthropic, OpenAI or other API is called; no such SDK is a dependency. The `ChatProvider` protocol is the seam an adapter would implement. |
| Redis | **Present but stores nothing.** It is constructed and health-checked; no code writes to it. It is not caching, queueing or session storage — see [ADR-003](docs/decisions/ADR-003-redis-role.md). Checkpoints live in PostgreSQL. |
| Container image | **Never built.** `infrastructure/docker/api.Dockerfile` and `docker-compose.yml` exist and CI validates `docker compose config`, but no CI job builds or publishes the image. |
| Completed workflows | **Not demonstrated over HTTP in CI.** The integration job runs no legacy portal, so the entitlement step cannot be confirmed and validation correctly declines to report success. The full billing → CRM → portal → validation-`PASS` chain is verified at node level against a stub portal. |
| Evaluation judge | AgentForge supports an LLM judge; CI deliberately never uses it. Scoring in CI is entirely deterministic. |

Everything else described on this page is exercised by the test suite.

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | Developed and tested on 3.11 |
| [uv](https://docs.astral.sh/uv/) | 0.11+ | Dependency and environment manager |
| PostgreSQL | 17 (13+ works) | **Must have the `pgvector` extension available** |
| Redis | 8 (5+ works) | |

Two supported ways to get PostgreSQL and Redis running. Pick one.

---

## Path A — Docker Compose (recommended, but see the warning)

> ⚠ **Not yet verified.** The compose file and Dockerfile were written on a
> machine without Docker installed, so they have never been executed. Image tags
> were checked against their registries and the configuration is complete, but
> treat the first run as a task to complete, not a formality. If it fails,
> that is a known-open item, not a surprise — see
> [docs/PHASE-01-COMPLETION.md](docs/PHASE-01-COMPLETION.md).

Requires Docker Desktop (which on Windows requires WSL2).

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

That starts exactly three services — `api`, `postgres`, `redis` — applies
migrations automatically on start, and serves the API on
<http://localhost:8000>.

---

## Path B — Local services, no Docker

### 1. PostgreSQL with pgvector

**Windows.** Install PostgreSQL from the
[EDB installer](https://www.postgresql.org/download/windows/) or
`winget install PostgreSQL.PostgreSQL.17`.

pgvector does **not** ship with that installer and has no prebuilt Windows
binary. Build it — this needs "Desktop development with C++" from Visual Studio
Build Tools, and an **x64 Native Tools Command Prompt run as Administrator**:

```bat
set "PGROOT=C:\Program Files\PostgreSQL\17"
cd %TEMP%
git clone --branch v0.8.6 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

**macOS:** `brew install postgresql@17 pgvector`
**Debian/Ubuntu:** `apt install postgresql-17 postgresql-17-pgvector`

Then create the database and role (adjust to match your `.env`):

```sql
CREATE ROLE custops WITH LOGIN PASSWORD 'change-me-locally';
CREATE DATABASE custops OWNER custops;
```

`CREATE EXTENSION vector` is run by the migration, not by hand — but it needs
sufficient privilege, so either grant the role superuser locally or run
migrations as a superuser. Splitting the migration role from a least-privilege
application role is not implemented; both still use one role.

### 2. Redis

Redis has no official Windows build. On Windows, choose one of:

- **[Memurai](https://www.memurai.com/)** — Redis-compatible Windows service;
  the Developer edition is free. `winget install Memurai.MemuraiDeveloper`
- **WSL2** — `wsl --install`, then `sudo apt install redis-server`
- **Docker** — `docker run -p 6379:6379 redis:8.10`

macOS: `brew install redis`. Linux: `apt install redis-server`.

### 3. Application

```bash
cp .env.example .env
```

Edit `.env` so `POSTGRES_*` and `REDIS_*` match what you just installed, then:

```bash
uv sync
```

```bash
uv run alembic upgrade head
```

Load the synthetic catalogue (seven accounts, each exercising a different
eligibility path — see [seed.py](src/custops/domain/seed.py)):

```bash
uv run custops seed
```

Embed the policies and contracts so they can be retrieved as evidence
(idempotent — unchanged documents are skipped):

```bash
uv run custops ingest
```

```bash
uv run uvicorn custops.apps.api.main:app --reload
```

---

## Verify it works

```bash
curl -i http://localhost:8000/health
```

Healthy — HTTP 200:

```json
{
  "status": "ok",
  "service": "custops-api",
  "version": "0.1.0",
  "environment": "local",
  "request_id": "6f1b…",
  "execution_id": null,
  "dependencies": {
    "postgres": {"status": "up", "latency_ms": 2.1,
                 "detail": {"server_version": "17.2", "pgvector_extension": "0.8.6"}},
    "redis":    {"status": "up", "latency_ms": 0.4,
                 "detail": {"redis_version": "8.10.0"}}
  }
}
```

If a dependency is unreachable you get **HTTP 503** with the *same* body shape,
`"status": "degraded"`, and an `error` explaining which dependency failed and
why. The API starts and answers even when both dependencies are down — a service
that refuses to boot cannot tell you what is wrong with it.

Interactive API docs: <http://localhost:8000/docs>

### Inspecting the systems of record

Read-only views onto the seeded data. Useful for seeing the deterministic
business rules in isolation, without starting a workflow:

```bash
curl "http://localhost:8000/enterprise/customers/ACME"
```

The verdict on a proposed upgrade — eligibility, proration and whether a human
must approve, with a source reference for every fact:

```bash
curl "http://localhost:8000/enterprise/accounts/<account_id>/upgrade-assessment?target_plan_code=enterprise"
```

Try it against the seeded accounts to see each branch: `ACME` proceeds
automatically, `GLOBEX` is blocked by its contract, `UMBRELLA` needs approval for
its 35% discount, `VEHEMENT` escalates on ambiguous contract wording.

Every enterprise route is a `GET`. Mutations are deliberately not exposed over
HTTP — they travel through the MCP tool layer, which verifies an approval record
before acting.

---

## Running a workflow

Everything below `/health` and `/enterprise` requires a bearer token. Mint one
for a seeded user — it is printed **once** and only its hash is stored:

```bash
uv run custops issue-token --email ops.approver@custops.example.com --label laptop
```

Export it:

```bash
export CUSTOPS_TOKEN=custops_...
```

Start a run. The `ops.approver` user holds the `operator` role, which is what
starting a workflow requires — it reaches billing, CRM and the legacy portal:

```bash
curl -X POST http://localhost:8000/workflows \
  -H "Authorization: Bearer $CUSTOPS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"request": "Upgrade ACME to the enterprise plan."}'
```

**`202` means it paused for a human** — the graph reached the approval gate and
interrupted. Try `UMBRELLA`, whose 35% discount trips the threshold.

**`201` means the run reached a terminal state without pausing** — it is *not* a
claim that the upgrade succeeded. Read `status` in the body for that:
`completed` is one outcome, `escalated` is another, and both return 201. A run
whose validation could not confirm every system of record escalates rather than
reporting success.

Reconstruct the full trace afterwards — graph steps, tool calls and audit events
merged into one ordered timeline:

```bash
curl -H "Authorization: Bearer $CUSTOPS_TOKEN" \
  "http://localhost:8000/workflows/<execution_id>"
```

### Approving a paused run

List what is waiting, then decide. Note the decision body carries **no
identity** — who approved comes from the token, never from the request:

```bash
curl -H "Authorization: Bearer $CUSTOPS_TOKEN" \
  "http://localhost:8000/approvals?status=pending"
```

```bash
curl -X POST "http://localhost:8000/approvals/<approval_id>/decision" \
  -H "Authorization: Bearer $CUSTOPS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "note": "Checked the contract."}'
```

High-value upgrades need `finance.approver@custops.example.com`; a plain
approver reaching the endpoint still cannot sign off past the threshold. A
`viewer@custops.example.com` token can read everything and decide nothing.

Revoke a credential when you are done with it:

```bash
uv run custops revoke-token --token-id <id>
```

---

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
```

**Skipped tests are expected without live services.** Integration and end-to-end
tests probe their dependencies and skip with the precise blocker named, rather
than failing — a skip reports "not exercised", while a failure would claim the
code is broken.

The suite is **864 tests**: 703 unit (no infrastructure), 154 integration
(PostgreSQL with pgvector, Redis) and 7 end-to-end (Chromium). Locally, without
services, the unit layer runs and the rest skip. CI runs all three, plus lint and
types, packaging, and compose validation — 6 jobs, with the evaluation gate as a
separate workflow.

The evaluation regression gate is separate, and deterministic — the LLM judge is
never used in CI, because a build check that fails when an API call times out is
worse than no check:

```bash
uv run custops evaluate --version "$(git rev-parse --short HEAD)"
```

It exits non-zero when [AgentForge](https://github.com/Harikrishnan006/agent-forge)
detects a regression against the committed baseline. `agent-forge` is a **dev
dependency** — a production install pulls neither it nor pandas, pyarrow or
google-genai, and a test asserts no production module imports it.

Run only one layer:

```bash
uv run pytest tests/unit
```

```bash
uv run pytest -m integration
```

### Migrations

```bash
uv run alembic upgrade head
```

```bash
uv run alembic revision --autogenerate -m "description"
```

Preview the SQL without touching a database — useful for review, and it works
with no server running at all:

```bash
uv run alembic upgrade head --sql
```

Every model must be imported in `src/custops/domain/models/__init__.py` or
autogenerate will silently not see it.

---

## Configuration

All configuration flows through Pydantic Settings in
[`src/custops/config.py`](src/custops/config.py). Nothing else reads
`os.environ`, which is what keeps the secret surface auditable.

`.env.example` documents every variable; `.env` is gitignored and must never be
committed. Infrastructure variables use conventional names (`POSTGRES_*`,
`REDIS_*`, `LOG_*`) so the same values work for Docker, a native install and CI;
application variables are namespaced `CUSTOPS_*`.

Set `LOG_FORMAT=console` for readable local logs; leave it `json` anywhere else.

---

## Layout

```
src/custops/               application code (ADR-001 explains why not at the root)
  agents/                  graph nodes, routing, state, schemas, budgets
  apps/api/                FastAPI app, routers, schemas, middleware, security
  apps/orchestrator/       graph assembly, workflow runner, checkpointer
  apps/enterprise/         read-only HTTP views onto the systems of record
  apps/billing_specialist/ the A2A specialist, run as its own process
  apps/legacy_portal/      the no-API portal that Playwright drives
  mcp/server/              MCP server and tool registration
  mcp/tools/               tool handlers, approval verification, results
  mcp/permissions/         tool/role permission matrix
  a2a/client/              specialist client
  a2a/contracts/           agent card and pricing contracts
  knowledge/ingestion/     chunking and embedding pipeline
  knowledge/retrieval/     pgvector search
  domain/models/           SQLAlchemy models
  domain/rules/            deterministic eligibility and pricing
  domain/policies/         retrieval, approval authority, budgets
  providers/               chat and embedding providers
  provisioning/            Playwright client and provisioning contracts
  evaluation/              AgentForge adapter, runner, golden datasets
  observability/           logging, correlation context, event catalogue, probes
  cache/                   Redis client and probe
  db/                      declarative base, engine, session factory
  cli.py                   operator commands
  config.py                the entire configuration surface
migrations/                Alembic (async), 7 revisions
tests/unit/                no infrastructure required
tests/integration/         requires PostgreSQL / Redis; skips when absent
tests/e2e/                 requires Chromium and the portal; skips when absent
benchmarks/                orchestration-framework comparison (dev only)
evaluation/baseline/       committed regression baseline for the gate
infrastructure/docker/     api.Dockerfile, entrypoint.sh
docs/                      architecture, decisions, phase reports
```
