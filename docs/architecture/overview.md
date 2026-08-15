# Architecture overview

> **Scope note.** This document describes what exists **as of Phase 1**, and
> marks everything else as planned. Documentation that describes unbuilt
> software as if it were built is worse than no documentation (Rules 20, 21).

## What the system is

An agentic platform for B2B SaaS customer operations. It receives
natural-language business requests and converts them into executable, stateful,
auditable workflows that produce **real state changes across real systems**, not
text.

The reference request, and the one workflow shipped end-to-end (decision D3):

> *"Upgrade Acme from Professional to Enterprise. Check eligibility, verify the
> contract, calculate pricing, update the CRM and billing system, and send
> confirmation."*

## Three protocols, three distinct jobs

The single most important thing to be able to state about this architecture, and
the thing most likely to be asked about:

| Concern | Technology | Question it answers |
|---|---|---|
| Workflow / state orchestration | **LangGraph** | What happens next, and what is the state? |
| Agent → agent communication | **A2A** | How does this agent ask *another autonomous agent* for a capability it does not own? |
| Agent → tool communication | **MCP** | How does an agent invoke a typed, permissioned capability? |

They are not interchangeable, and none is a transport detail of another.
LangGraph holds the state machine. A2A crosses an ownership boundary to another
agent (the Billing Specialist, decision D6 — a separate process, own port, own
capability contract). MCP is how any agent touches an enterprise system at all;
agents get no arbitrary database access.

## Current state (Phase 1)

```
┌──────────────────────────────────────────────┐
│ api (FastAPI)                                │
│   RequestContextMiddleware → request_id      │
│   GET /health                                │
│     ├── probe → postgres  (SELECT 1, pgvector version)
│     └── probe → redis     (PING)             │
│   structured JSON logging, execution_id field│
└──────────────────────────────────────────────┘
         │                        │
   ┌─────▼──────┐          ┌──────▼─────┐
   │ postgres   │          │ redis      │
   │ + pgvector │          │ (role TBD, │
   │ 4 tables   │          │  ADR-003)  │
   └────────────┘          └────────────┘
```

Implemented:

- **Configuration** — one Pydantic Settings surface; nothing reads `os.environ`.
- **Structured logging** — JSON, one pipeline for application *and* third-party
  records, `execution_id` present on every line (null until Phase 5 sets it).
- **Database** — async SQLAlchemy 2.0, Alembic migrations, pgvector enabled,
  identity + audit foundation tables.
- **Health** — live, bounded, concurrent dependency probes; 200/503.

Not implemented, and deliberately absent rather than stubbed (Rule 6): every
agent, the graph, MCP, A2A, the legacy portal, Playwright, evaluation.

## Target architecture

```
USER REQUEST
    ↓ SUPERVISOR      classify, route, monitor
    ↓ PLANNER         structured execution plan (Pydantic structured output)
    ↓ RESEARCH        RAG + system-of-record evidence, with source references
    ↓ DECISION        deterministic rules + LLM reasoning
    ↓ APPROVAL GATE   if risk thresholds exceeded (graph interrupt)
    ↓ EXECUTION       MCP tools + A2A billing specialist + Playwright
    ↓ VALIDATION      expected vs actual, across every affected system
    ↓ RETRY / REPLAN  bounded by configuration, enforced in Python
    ↓ AUDIT + RESULT
```

### The honesty check that shapes the design

The legacy provisioning portal (decision D8) has **no API**; entitlements are
authoritative there and must be flipped through a browser. This creates the
system's central engineering point:

> The billing API can return `200` while the entitlement never flipped.
> **API success ≠ business success.**

That is why the Validator re-reads state from every system of record rather than
trusting the executing agent's return value (§14), and why validation is a graph
node rather than an assertion inside the execution step.

### Where the LLM is, and is not

| LLM | Deterministic Python |
|---|---|
| Language understanding, classification | Pricing, proration, financial arithmetic |
| Plan generation | Approval and discount thresholds |
| Document / policy interpretation | Permission and authorization checks |
| Evidence synthesis, rationale | State-transition legality, retry/replan budgets |

Enforced structurally: rules live in `domain/rules/` and `domain/policies/`, are
called from Python, and are never exposed as tools a model can influence. **An
LLM cannot override an authorization or safety rule** (§12).

Approval is enforced in *three* layers, of which the third is the real one
(decision D9): the graph routes to an approval gate; the API records the human
decision; and **every mutating MCP tool independently verifies an approval
record in PostgreSQL before acting**. The graph is a happy path; the tool is the
boundary. A test calls the tool directly, bypassing the graph, and asserts
rejection.

## Layout

See [ADR-001](../decisions/ADR-001-repository-layout.md) for why application code
sits under `src/custops/` rather than at the repository root.

```
src/custops/
    apps/api/          FastAPI entrypoint            ✅ Phase 1
    apps/orchestrator/ LangGraph runtime             ◻ Phase 5
    apps/enterprise/   CRM / billing / support       ◻ Phase 2  (one service, D5)
    apps/legacy_portal/ API-less provisioning portal ◻ Phase 8
    agents/            supervisor, planner, research, execution, validator,
                       billing_specialist            ◻ Phases 5, 9
    a2a/               contracts/, client/           ◻ Phase 9
    mcp/               server/, tools/, permissions/ ◻ Phase 4
    workflows/         subscription_upgrade/         ◻ Phase 6
    domain/models/     SQLAlchemy models             ✅ Phase 1 (foundation only)
    domain/rules/      deterministic business rules  ◻ Phase 2
    domain/policies/   thresholds, permissions       ◻ Phase 7
    knowledge/         ingestion/, retrieval/        ◻ Phase 3
    providers/         model provider abstraction    ◻ Phase 5  (D11: pluggable)
    observability/     logging, context, events      ✅ Phase 1
    evaluation/        orchestrator evaluators       ◻ Phase 11 (D10: imports
                                                       llm-agent-eval-platform)
```

Directories arrive with the phase that fills them; empty packages are not
created in advance.

## Decision record

| ADR | Subject | Status |
|---|---|---|
| [001](../decisions/ADR-001-repository-layout.md) | `src/custops/` layout | Accepted |
| [002](../decisions/ADR-002-postgres-with-pgvector.md) | One PostgreSQL, pgvector as extension | Accepted |
| [003](../decisions/ADR-003-redis-role.md) | What Redis is for | 🟡 Open — Phase 5 |
| 004 | Orchestration framework: LangGraph vs CrewAI | Reserved — Phase 10 (§10) |
| [005](../decisions/ADR-005-pgvector-index-type.md) | HNSW over IVFFlat for the pgvector index | Accepted |
