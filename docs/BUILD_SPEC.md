# AI Customer Operations Orchestrator — Build Specification

> **How to use this document.** Paste this entire file into Claude Code as the opening message of a fresh session in an empty repository. Then implement **Phase 1 only** and stop.
>
> Keep this file in the repo at `docs/BUILD_SPEC.md` and update it as decisions change. It is the single source of truth for architecture.

---

## 0. Role and mandate

You are the lead Staff AI Engineer and Software Architect for this project. Build it as a serious, portfolio-quality enterprise AI engineering system.

The goal is **not** the largest possible AI project. The goal is a technically credible enterprise agentic system where every component survives the question *"why is this here?"*

---

## 1. Project purpose

An enterprise-style agentic platform for B2B SaaS customer operations. It receives natural-language business requests and converts them into executable, stateful, auditable workflows that produce **real state changes**, not text.

```
USER REQUEST
    ↓ SUPERVISOR      (classify, route, monitor)
    ↓ PLANNER         (structured execution plan)
    ↓ RESEARCH        (RAG + system-of-record evidence)
    ↓ DECISION        (deterministic rules + LLM reasoning)
    ↓ APPROVAL GATE   (if risk thresholds exceeded)
    ↓ EXECUTION       (MCP tools + Playwright)
    ↓ VALIDATION      (expected vs actual, across systems)
    ↓ RETRY / REPLAN  (bounded)
    ↓ AUDIT + RESULT
```

**Business problem.** Customer operations teams manually coordinate across CRM, billing, subscriptions, support, contracts, policies, and legacy web apps. Example request:

> *"Upgrade Acme from Professional to Enterprise. Check eligibility, verify the contract, calculate pricing, update the CRM and billing system, and send confirmation."*

**Industry.** B2B SaaS / Enterprise Customer Operations. Use synthetic but realistic seeded data. Never depend on real customer data.

---

## 2. Locked architectural decisions

These were decided deliberately. Do not silently revise them. If you believe one is wrong, **stop and raise it** rather than implementing an alternative.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Greenfield repository.** No existing code to inspect or migrate. | Fresh start; no legacy constraints. |
| D2 | **Celery is cut.** Redis stays. | No long-running job justified it. Redis earns its place as the LangGraph checkpoint/cache backend alone. Adding Celery would be resume-driven, violating Rule 7. |
| D3 | **One workflow ships end-to-end before any second workflow begins.** Subscription Upgrade only. Billing Dispute and Renewal Risk are deferred. | Depth over breadth. Three workflows are variations on one graph; they add ~35% build time for marginal demonstrated capability. |
| D4 | **pgvector is a Postgres extension, not a service.** One database container. | pgvector ships as `CREATE EXTENSION vector`. Two DB services in a compose file is a visible architectural error. |
| D5 | **CRM / Billing / Support are domain modules inside one `enterprise` service**, not three microservices. | Three near-identical CRUD services is padding. Domain boundaries are enforced by module structure and MCP tool scoping, which is what actually matters. |
| D6 | **A2A specialist = Billing Specialist Agent**, running out-of-process with its own port, its own capability contract, and independent startup. | Billing is the one capability with a genuine ownership boundary in real B2B SaaS (finance systems team), and it is consumed by more than one workflow. A2A here represents a real cross-boundary call, not two modules talking over HTTP. |
| D7 | **CrewAI is a measured comparison, not a production path.** The evidence-research subflow is implemented twice — LangGraph fan-out and CrewAI crew — instrumented, compared, and resolved in an ADR. | The measurement *is* the deliverable. This converts a keyword into evidence of framework-selection discipline. |
| D8 | **The legacy system is an entitlement/provisioning portal** with no API. Plan tier changes must be flipped there via browser automation. | Creates a genuine reason for Playwright *and* makes the Validator necessary: the billing API can return 200 while entitlement never flipped. API success ≠ business success. |
| D9 | **Approval is enforced in the tool layer, not the graph.** MCP mutating tools independently verify an approval record in Postgres. | Defense in depth. Even if the LLM routes around the approval node, the tool refuses. The graph is a happy path; the tool is the boundary. |
| D10 | **Phase 11 imports `llm-agent-eval-platform` as a dependency.** Do not reimplement scoring, LLM-as-a-Judge, or regression gating. | The library already exists and is tested. Reuse proves it is reusable engineering. See §15 note. |
| D11 | **Model provider layer is pluggable via config.** OpenAI, Anthropic, Google implemented. | Adding a fourth provider (e.g. Azure OpenAI) must be a config + adapter change, never a business-logic change. |

---

## 3. Technology stack — final

| Layer | Choice |
|---|---|
| Language | Python 3.11+, type hints throughout |
| Backend | FastAPI |
| Orchestration | LangGraph (primary, non-negotiable) |
| AI abstractions | LangChain where genuinely useful |
| Schemas | Pydantic v2 |
| Database | PostgreSQL (source of truth) |
| Vector search | pgvector extension |
| Cache / checkpoints | Redis |
| Tool protocol | MCP |
| Agent-to-agent | A2A |
| Browser automation | Playwright |
| Comparison framework | CrewAI (bounded, §10) |
| Containers | Docker + Docker Compose |
| Migrations | Alembic |
| Testing | pytest, Playwright |
| CI | GitHub Actions |
| Evaluation | `llm-agent-eval-platform` (imported) |

**Explicitly excluded:** Celery, Snowflake, Databricks, Airflow, Kafka, Flink, LlamaIndex. None have a justified purpose here.

---

## 4. Repository structure

```
apps/
    api/                    FastAPI entrypoint, auth, workflow + approval endpoints
    orchestrator/           LangGraph runtime
    enterprise/             CRM, billing, support domain modules + routers
    legacy_portal/          Simulated legacy provisioning app (no API)
agents/
    supervisor/
    planner/
    research/
    execution/
    validator/
    billing_specialist/     A2A server-side specialist
a2a/
    contracts/              Capability contracts / agent card schemas
    client/                 A2A client used by the Execution agent
mcp/
    server/                 MCP server
    tools/                  Typed tool implementations
    permissions/            Tool-level permission matrix
workflows/
    subscription_upgrade/
domain/
    models/                 SQLAlchemy + Pydantic models
    rules/                  Deterministic business rules
    policies/               Approval thresholds, permission policy
knowledge/
    ingestion/              Chunking, embedding, indexing
    retrieval/              pgvector retrieval, Evidence assembly
providers/                  Model provider abstraction
observability/              Structured events, audit
evaluation/                 Datasets + orchestrator-specific evaluators
tests/
    unit/ integration/ e2e/
infrastructure/
    docker/ scripts/
docs/
    architecture/ workflows/ decisions/
```

Adjust only with stated justification.

> **Phase 1 adjustment.** Application code lives under `src/custops/`, preserving
> every directory above inside it. Rationale: top-level `mcp/` and `a2a/` shadow
> the `mcp` and `a2a-sdk` import names required in Phases 4 and 9. See
> [ADR-001](decisions/ADR-001-repository-layout.md).

---

## 5. Domain model and database

PostgreSQL is the source of truth. Entities:

```
users, roles
customers, accounts, contacts
plans, subscriptions, invoices, payments, discounts
support_tickets, conversations
contracts, policies
knowledge_documents (+ embedding vector column)
workflow_executions, workflow_steps
agent_runs, tool_calls
approvals
audit_events
entitlements          -- authoritative in the legacy portal, mirrored here for validation
```

Use foreign keys and indexes on the columns workflows actually query. Do not over-engineer — design around the Subscription Upgrade flow, extend later.

`knowledge_documents` carries a `vector` column via pgvector. Choose the index type (HNSW or IVFFlat) based on the current pgvector documentation and record the choice in an ADR.

**Seed data** must include enough variety to exercise failure paths: an inactive account, a customer with a contract restriction blocking upgrade, a discount above threshold, a customer with a rich support history.

---

## 6. Agents

Five agents with distinct, non-overlapping responsibilities.

**Supervisor** — *"What needs to happen?"* Classifies the request, identifies workflow type and required capabilities, monitors progress, routes failures, determines completion. Must **not** perform unrestricted business actions.

**Planner** — *"How should this be accomplished?"* Converts natural language into a structured plan via Pydantic structured output. Identifies steps, tools, dependencies, parallelisable operations, and likely approval requirements.

```json
{
  "workflow_type": "subscription_upgrade",
  "steps": ["identify_customer", "retrieve_subscription", "retrieve_contract",
            "retrieve_pricing", "check_eligibility", "calculate_price",
            "check_approval", "update_subscription", "flip_entitlement",
            "update_crm", "validate", "notify"]
}
```

**Research** — *"What do we know, and what evidence supports it?"* Retrieves customer, subscription, contract, policy, pricing, and support data from Postgres + pgvector. Returns **structured evidence with source references**, never prose.

**Execution** — Executes approved actions only. Selects tools, calls MCP, calls the A2A billing specialist, drives Playwright for browser-only systems. Tool access is permission-controlled. Must never bypass authorization or approval.

Execution strategy:

```
API available?  → YES → MCP tool
                → NO  → browser-only? → YES → Playwright
```

**Validator** — Verifies expected vs actual state across *all* affected systems. Returns `PASS` / `FAIL` / `NEEDS_REVIEW` and recommends retry or replan. Never assumes a 200 response means the business outcome occurred.

---

## 7. LangGraph design

### WorkflowState

Strongly typed. Use LangGraph's reducer mechanism for accumulating fields (evidence, tool calls, errors). **Verify the current reducer/annotation API against LangGraph docs before implementing** — do not assume from memory.

```
execution_id, request_id, raw_request
workflow_type, plan
customer_ref, evidence[]           (append)
decisions[], tool_calls[], tool_results[]   (append)
approval_status, approval_id
execution_results[], validation_results[]   (append)
errors[]                           (append)
retry_count, replan_count, status, metadata
```

**Never store chain-of-thought.** Store structured decisions, evidence, tool I/O where safe, validation results, and concise rationale summaries.

### Graph topology

```
supervisor → planner → research → decide
                                    ├─ requires_approval → approval_gate → execute
                                    └─ auto                              → execute
execute → validate
validate ├─ PASS                          → notify → complete
         ├─ FAIL & retry_count < MAX      → execute
         ├─ FAIL & replan_count < MAX     → planner
         └─ FAIL & budgets exhausted      → escalate
research ├─ evidence_sufficient           → decide
         └─ low retrieval confidence      → escalate
```

Retry and replan budgets are **configuration values enforced in Python**, not LLM decisions.

### Checkpointing and interrupts

Persist checkpoints so a workflow paused for human approval survives a process restart. LangGraph provides Postgres checkpointer packages and an interrupt mechanism for human-in-the-loop — **look up the current package name, class name, and interrupt API in the LangGraph documentation before writing code.** Do not guess at these; the API has changed across versions.

---

## 8. MCP design — agent → tool

Standardised, narrowly scoped access to enterprise capabilities. Agents get **no arbitrary database access.**

Tools: `get_customer`, `get_subscription`, `update_subscription`, `get_contract`, `get_pricing`, `get_invoice`, `create_refund`, `get_support_history`, `update_crm`, `search_knowledge`, `send_notification`

Requirements:

- Every tool has a Pydantic input schema and a Pydantic output schema
- Every tool declares a required permission; the permission matrix lives in `mcp/permissions/`
- Every mutating tool writes a `tool_calls` row and an `audit_events` row
- **Every mutating tool independently verifies an approval record exists and is `APPROVED` for this `execution_id` + action, and rejects otherwise** (D9)
- Tool errors return structured failures, never raw exceptions

Verify the current Python MCP SDK package name and server API against the official MCP documentation before implementing.

---

## 9. A2A design — agent → agent

**Do not implement this as "two of my services calling each other over HTTP."** That is the failure mode this section exists to prevent.

The Billing Specialist Agent must:

- Run as a **separate process** with its own port and its own Dockerfile
- Be independently startable — the orchestrator degrades gracefully if it is down
- Publish a capability/discovery contract per the A2A specification
- Expose billing reasoning as a capability: given subscription + contract + pricing policy, return a structured pricing decision with rationale and confidence
- Own its own tool access; the orchestrator never reaches into billing tools directly

**Before implementing, read the current published A2A specification** (agent cards, task lifecycle, message format). The protocol has evolved and this document may be stale. Conformance to the actual spec is the entire point.

Keep the three concerns distinct and say so in the architecture docs:

- **LangGraph** = workflow / state orchestration
- **A2A** = agent-to-agent communication
- **MCP** = agent-to-tool communication

---

## 10. CrewAI — measured comparison mandate

CrewAI does **not** replace LangGraph and does **not** run in the production path.

Implement the **evidence-research subflow** twice:

1. **LangGraph version** — parallel node fan-out across subscription, contract, pricing, policy, and support retrieval, with a synthesis step
2. **CrewAI version** — a crew of specialist researcher roles with a synthesis task

Run both across the same input set. Instrument and record:

- wall-clock latency (p50, p95)
- token count in / out
- estimated cost
- evidence completeness against a labelled ground truth
- failure modes under a degraded tool (simulate one retrieval source erroring)

Deliverable: `docs/decisions/ADR-004-orchestration-framework.md` recording the measurement, the decision, and the reasoning. **The measurement is the deliverable.** The expected outcome is "LangGraph stays" — but the data decides, and a documented surprise is a better result than an assumed conclusion.

---

## 11. Playwright and the legacy portal

Build a simulated legacy provisioning portal — server-rendered, session-cookie auth, form-driven, **no API**. It is the authoritative store for `entitlements`.

The Execution agent must drive it via Playwright: browser startup → login → navigate → locate the account → submit the tier change form → extract confirmation → verify.

This creates the architecture's most important honesty check: **the billing API can succeed while entitlement never flipped.** The Validator must query both and fail the workflow on divergence. Write at least one test that forces exactly this divergence.

Playwright in Docker requires system dependencies — use the official Playwright base image or install browser dependencies explicitly. Verify the current image tag before building.

---

## 12. Deterministic vs LLM boundary

**LLM handles:** natural-language understanding, request classification, plan generation, document and policy interpretation, evidence synthesis, contextual reasoning, notification drafting.

**Deterministic Python handles:** pricing calculations, proration, discount thresholds, approval thresholds, permission checks, financial arithmetic, state transition legality, safety constraints, authorization, retry/replan budgets, and every validation comparison that can be expressed as a rule.

**An LLM must never be able to override a deterministic authorization or safety rule.** This is enforced structurally — the rules live in `domain/rules/` and `domain/policies/`, are called from Python, and are not exposed as tools the model can influence.

---

## 13. Human-in-the-loop

Approval is required for configurable high-risk actions: large refunds, discounts above threshold, destructive actions, ambiguous contract terms, low-confidence decisions, policy exceptions.

An approval request contains: entity, proposed action, reason, supporting evidence, risk assessment, expected outcome.

Enforcement is layered:

1. The graph routes to an approval gate and interrupts
2. The approval API records the human decision with actor and timestamp
3. **The MCP mutating tool independently verifies the approval record before acting** (D9)

Layer 3 is what makes this real. Test it by calling the tool directly, bypassing the graph entirely, and asserting rejection.

---

## 14. Validation strategy

Every significant action has a matching validation.

```
Action:      update subscription Professional → Enterprise
Validation:  re-read subscription from billing        → plan == enterprise, status active
             re-read entitlement from legacy portal   → tier == enterprise
             re-read CRM account                      → reflects new plan
             re-check invoice/proration               → matches deterministic calculation
Result:      PASS only if all four agree
```

Validation reads from the systems of record, never from the execution agent's own return value.

---

## 15. Evaluation

> **Assumption flagged.** You selected "Fresh repo" rather than the option that bundled the eval import. I am proceeding on the reading that you meant *fresh repo, and still import the eval platform at Phase 11.* If you actually intend to rebuild the harness from scratch, say so — it is roughly 20 additional hours and a weaker story.

Import `llm-agent-eval-platform` from GitHub as a dependency. Do not reimplement deterministic scoring, LLM-as-a-Judge, or regression gating.

Add **orchestrator-specific evaluators** on top:

- workflow completion rate
- planning accuracy (predicted steps vs ground-truth plan)
- retrieval quality (evidence precision/recall against labelled set)
- tool selection accuracy and tool-call correctness
- validation accuracy (did the Validator catch injected divergences?)
- retry rate, escalation rate
- latency, token usage, estimated cost

Synthetic datasets must include adversarial cases: customer not found, inactive account, contract restriction, discount above threshold, billing API timeout, CRM update failure, entitlement/billing divergence, malformed contract document, low retrieval confidence, approval rejection, browser failure.

Regression evaluation must be runnable after any code or model change and must gate CI.

---

## 16. Observability

Every workflow gets a unique `execution_id`, propagated through every log line, tool call, agent run, and audit event.

Structured events: `request_received`, `workflow_classified`, `plan_created`, `retrieval_started`, `retrieval_completed`, `tool_selected`, `tool_called`, `tool_completed`, `a2a_request_sent`, `a2a_response_received`, `decision_made`, `approval_requested`, `approval_received`, `validation_started`, `validation_completed`, `retry`, `replan`, `workflow_completed`, `workflow_failed`.

Store structured decisions, evidence, tool I/O where safe, validation results, and concise rationale. **Never store or expose chain-of-thought.**

Provide an inspection endpoint that reconstructs a full workflow trace from `execution_id`.

---

## 17. Security

Authentication, role-based authorization, tool-level permissions, environment-based secrets, input validation, approval enforcement, audit logging, safe action boundaries.

An agent must never: modify arbitrary database records, bypass authorization, bypass approval, access secrets, or execute shell commands.

No hardcoded secrets. `.env.example` committed; `.env` gitignored.

---

## 18. Testing

pytest for unit and integration; Playwright for browser E2E.

Cover: business rules, Pydantic schemas, API endpoints, MCP tools, tool permissions, LangGraph nodes, routing logic, state transitions, retry and replan budgets, validation, approval enforcement (including direct-tool bypass attempts), RAG retrieval, A2A communication, Playwright automation, end-to-end workflows.

Failure tests are mandatory, not optional: API timeout, invalid tool result, missing customer, conflicting records, malformed document, low retrieval confidence, validation failure, approval rejection, browser failure, A2A specialist unavailable.

---

## 19. Working rules

1. Do not generate the entire project in one shot.
2. Build incrementally in defined phases.
3. Inspect existing code before extending it.
4. Never overwrite working functionality without a stated reason.
5. Preserve backward compatibility where practical.
6. Do not create fake implementations to satisfy a technology requirement.
7. Do not add technologies for resume keywords.
8. Every agent, service, protocol, and dependency must have a technical purpose.
9. Prefer simple, maintainable architecture over unnecessary complexity.
10. Production-quality Python throughout.
11. Type hints throughout.
12. Pydantic for structured contracts.
13. Write tests alongside implementation.
14. Run tests after meaningful changes.
15. Diagnose and fix failures; never bypass a test.
16. No hardcoded secrets, keys, or credentials.
17. Environment variables and `.env.example`.
18. Do not expose chain-of-thought.
19. Store structured reasoning summaries, evidence, decisions, and tool results.
20. Do not claim a feature works until it actually works.
21. Keep documentation synchronised with implementation.
22. Use clear commit-sized conceptual changes.
23. **After each phase, the human explains the design aloud without looking at the code. Anything they cannot explain gets rewritten by hand.** At the end of every phase, produce a short "what you should be able to explain" checklist to support this.
24. **When a library API is uncertain, look it up in current documentation. Never invent function names, class names, or package names.**

---

## 20. Phase plan

Estimates are rough and assume ~25 hrs/week. Treat them as planning aids, not commitments.

| Phase | Scope | Est. |
|---|---|---|
| **1** | Foundation: structure, config, Docker, Postgres + pgvector, Alembic, logging, pytest | 8–12h |
| **2** | Domain models, enterprise service (CRM/billing/support), seed data | 15–20h |
| **3** | Knowledge & RAG: ingestion, chunking, embeddings, pgvector retrieval, Evidence model | 15–20h |
| **4** | MCP server, typed tools, permission matrix | 15–20h |
| **5** | LangGraph core: state, five agent nodes, conditional edges, retry/replan, checkpointing | 25–35h |
| **6** | Subscription Upgrade end-to-end, API path only | 15–20h |
| **7** | Human-in-the-loop: approval model, API, three-layer enforcement | 12–18h |
| **8** | Legacy portal + Playwright execution + cross-system validation | 20–25h |
| **9** | A2A Billing Specialist, out-of-process | 15–20h |
| **10** | CrewAI comparison + instrumentation + ADR | 12–18h |
| **11** | Evaluation: import eval platform, orchestrator evaluators, datasets | 12–18h |
| **12** | Observability: traces, audit, inspection endpoint | 10–15h |
| **13** | Security hardening | 10–15h |
| **14** | CI/CD, final documentation, architecture diagrams | 8–12h |

**Total: ~190–270 hours ≈ 8–11 weeks at 25 hrs/week.**

Note the ordering change from the original draft: **MCP is built before the graph** (Phase 4, not Phase 6). Agents need real tools to call; building agents first means building tool interfaces twice.

At the end of each phase: run tests, verify services start, document what was completed, document known issues, list files changed, produce the Rule 23 explanation checklist — **then stop and wait for approval.**

---

## 21. Phase 1 — definition of done

Phase 1 is complete only when **all** of the following are true and demonstrated:

1. `docker compose up` starts exactly three services: `api`, `postgres`, `redis`. Nothing else.
2. Postgres runs with the pgvector extension available and `CREATE EXTENSION IF NOT EXISTS vector` applied via migration.
3. Alembic migration runs clean from empty and creates the base tables.
4. `GET /health` returns 200 and independently confirms Postgres connectivity and Redis connectivity in its response body.
5. Configuration loads through Pydantic Settings from environment. `.env.example` committed; `.env` in `.gitignore`.
6. Structured JSON logging is wired, with an `execution_id` field present in the log schema even though nothing generates one yet.
7. `pytest` passes with at least three tests: database connectivity, settings loading, `/health`.
8. `README.md` takes a stranger from clone to running system with no undocumented steps.

Then **stop.** Do not begin Phase 2.

---

## 22. Explicitly out of scope

Deferred (revisit only after Phase 14): Workflow 2 (Billing Dispute), Workflow 3 (Renewal Risk).

Excluded entirely: Celery, Snowflake, Databricks, Airflow, Kafka, Flink, LlamaIndex, and any cloud-provider-specific deployment. If you believe one is now justified, raise it — do not add it.

---

## 23. Quality standard

Review every decision as a senior AI engineering hiring manager would:

- Is this genuinely useful, or is it decoration?
- Is the LLM being used where it adds value, and deterministic code where it does not?
- Does this component have one clear responsibility?
- Can it fail safely?
- Can we validate the result independently of the actor that produced it?
- Can we explain what happened after the fact?
- Can we test it?
- Would another engineer understand this code?
- **Would this hold up under line-by-line questioning in a technical interview?**

---

## START

This is a greenfield repository. Do not attempt to inventory existing code.

1. Propose the concrete repository structure and Phase 1 file manifest.
2. Wait for confirmation.
3. Implement **Phase 1 only**.
4. Run tests. Verify services start. Verify database connectivity.
5. Report: what was completed, what failed, files changed, Rule 23 explanation checklist.
6. **Stop and wait for approval before Phase 2.**

Do not skip phases. Do not silently redesign. Do not add dependencies.
