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

> ### Current status: Phase 1 of 14 complete
>
> Phase 1 is **foundation only**: configuration, structured logging, database,
> migrations, health checking, tests. There are no agents, no LangGraph, no MCP
> tools and no workflows yet — they arrive in their own phases and are
> deliberately absent rather than stubbed. See
> [docs/PHASE-01-COMPLETION.md](docs/PHASE-01-COMPLETION.md) for exactly what is
> built, what is verified, and what is not.

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
migrations as a superuser. (Splitting the migration role from a least-privilege
application role is Phase 13 work.)

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

---

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
```

**Skipped tests are expected without live services.** Integration tests check
whether PostgreSQL and Redis are reachable and skip with a reason if they are
not, rather than failing — a skip reports "not exercised", while a failure would
claim the code is broken. With no services running you should see roughly
`36 passed, 10 skipped`; with both running, all 46 pass.

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
src/custops/         application code (see ADR-001 for why it is not at the root)
  apps/api/          FastAPI app, routers, schemas, middleware
  cache/             Redis client and probe
  db/                declarative base, engine, session factory
  domain/models/     SQLAlchemy models
  observability/     logging, correlation context, event catalogue, probes
  config.py          the entire configuration surface
migrations/          Alembic (async)
tests/unit/          no infrastructure required
tests/integration/   requires live PostgreSQL / Redis; skips when absent
infrastructure/      Dockerfile, entrypoint
docs/                architecture, decisions, phase reports
```
