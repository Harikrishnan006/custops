# Phase 9 — completion report

**Date:** 2026-08-15
**Scope:** The Billing Specialist as a genuinely out-of-process A2A participant
**Status:** Code complete. Contract, client behaviour and permission boundary verified;
network and database paths pending PostgreSQL.

---

## What was built

| Piece | Where |
|---|---|
| Capability payloads | `a2a/contracts/pricing.py` |
| Agent card (SDK protobuf types) | `a2a/contracts/card.py` |
| The specialist's A2A surface | `apps/billing_specialist/app.py` |
| Its reasoning, through its own MCP role | `apps/billing_specialist/reasoning.py` |
| Orchestrator-side client + degradation | `a2a/client/billing.py` |
| Consultation wired into `decide` | `agents/nodes.py` |
| `A2A_*` settings | `config.py` |
| Separate entry point | `pyproject.toml` → `custops-billing-specialist` |

---

## What makes it genuinely out-of-process

Not the transport — a simulated boundary can speak HTTP to itself. Four things:

1. **Its own process and port**, with its own entry point. It starts with
   nothing else in the platform running; it needs PostgreSQL and no more.
2. **Its own tool access.** It reads billing state through `execute_tool` under
   `Role.BILLING_SPECIALIST`, leaving `tool_calls` and `audit_events` rows
   attributed to *itself*. The orchestrator never fetches data on its behalf.
3. **Identifiers, not data, cross the boundary.** The request names an account
   and a target plan. A caller cannot influence the answer by choosing what to
   hand over.
4. **The orchestrator degrades.** With the specialist off, unreachable, or
   returning a malformed body, the workflow reaches the same decision from the
   same local rules and records that it went unconsulted.

`tests/integration/test_a2a_billing_specialist.py::TestOutOfProcess` starts the
agent as a real subprocess on a real port and drives it over a socket, so the
claim is falsifiable rather than asserted.

---

## The design question the phase actually turned on

Transport was the easy part. The question that mattered: **what may an
out-of-process agent influence?**

The specialist's role can read subscriptions, plans, contracts and invoices. It
*cannot* read customer records, and no tool exposes negotiated discounts. Its
view is structurally narrower than the orchestrator's.

Three consequences, recorded in **ADR-006**:

- Its verdict fields are named `billing_eligible` and `approval_indicated` —
  never `eligible` / `requires_approval`. A field called `eligible` invites one
  line of code that would upgrade a churned customer.
- It may **raise** an approval gate; it can never lower one. Its "no" is
  informed; its "yes" merely has not seen what it cannot see.
- The local figure governs the mutation. A divergence escalates to a human
  rather than being silently resolved — both sides run the same arithmetic, so
  different figures mean different state, and neither side can tell which
  reading is stale.

Widening the permission matrix to make the answer look complete was rejected:
the boundary is worth more than the completeness.

---

## A Phase 8 gap found and closed

Wiring the specialist into `WorkflowRunner` meant reading how `NodeDependencies`
is assembled there — and it was assembling only three of them:

```python
deps = NodeDependencies(
    session_factory=..., chat=..., embedder=...,   # provisioning never passed
)
```

Phase 8 built `PlaywrightProvisioningClient` and wired it through the tool layer,
but the runner still passed `provisioning=None`. Every API-driven run therefore
reached the provisioning step with no client, failed it, and was marked a
validation failure for a missing entitlement — **the exact outcome Phase 8
existed to eliminate.** Phase 8's tests did not catch it because they construct
`NodeDependencies` directly and pass the stub.

The runner now defaults to the real portal driver, with both it and the
specialist injectable so tests can substitute labelled doubles.

---

## Something else fixed along the way

Installing `a2a-sdk` populated the shared `google` namespace (via
`googleapis-common-protos`), which broke `mypy --strict` on
`providers/google_provider.py` — `from google import genai` became "module
`google` has no attribute `genai`" for an optional package that was never
installed. Changed to `importlib.import_module("google.genai")` inside the
existing `ImportError` guard: honest about being a dynamic optional import, and
stable whether or not some other distribution owns the namespace.

---

## Verification

```
ruff check      clean
mypy --strict   clean, 154 files
pytest          386 passed, 146 skipped   (was 338 / 129)
```

**48 new tests pass without infrastructure:** the agent card's published shape,
contract validation on both sides, the specialist's endpoints (discovery,
liveness, malformed payloads, task retrieval, per-app task isolation), the
client's three-way split between answered / refused / unavailable, the
"local figure governs" rule, the `A2A_ENABLED` wiring, and the permission
assertions — including one that reads the reasoning module's source and fails if
it ever uses a tool the role may not call.

**17 new tests are pending infrastructure**, marked and skipped with the precise
blocker named:

- PostgreSQL (`role "custops" does not exist` → `InvalidPasswordError`):
  the specialist's reads through its own role, audit attribution, agreement with
  the orchestrator's own assessment, the subprocess/network tests, and the
  `decide`-node integration (agreement, divergence, gate-raising, degradation).

None were weakened to go green.

---

## Still outstanding

- `tests/integration/test_database.py:23` asserts
  `HEAD_REVISION = "0002_foundation_tables"`; the actual head is
  `0006_workflow_executions`. Known, documented in
  `docs/INTEGRATION-VERIFICATION.md`, still unfixed.
- Phases 10–14: CrewAI comparison + ADR-004, evaluation platform, observability,
  security hardening, CI/CD and final documentation.
- Streamlit remains deferred.

---

## Running the specialist

```bash
custops-billing-specialist
```

Then point the orchestrator at it:

```
A2A_ENABLED=true
A2A_BILLING_SPECIALIST_URL=http://127.0.0.1:8200
```

`A2A_ENABLED` defaults to `false` deliberately: a main workflow that silently
depends on an optional process being up is an undeclared hard dependency.
