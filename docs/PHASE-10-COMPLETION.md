# Phase 10 — completion report

**Date:** 2026-08-15
**Scope:** CrewAI measured comparison + ADR-004
**Status:** Complete. The benchmark runs offline and its results are reproducible.

---

## What was built

| Piece | Where |
|---|---|
| Shared sources, inputs, ground truth, metering | `benchmarks/research_comparison/harness.py` |
| LangGraph parallel fan-out | `benchmarks/research_comparison/langgraph_flow.py` |
| CrewAI researcher crew | `benchmarks/research_comparison/crewai_flow.py` |
| Benchmark runner | `benchmarks/research_comparison/run.py` |
| Recorded results | `benchmarks/results/research_comparison.{json,md}` |
| The decision | `docs/decisions/ADR-004-orchestration-framework.md` |

**No production code changed in this phase.** `benchmarks/` sits outside
`src/custops/` and CrewAI is a dev-only dependency, so D7's "not a production
path" is structural rather than a promise.

---

## The decision

**LangGraph stays.** Both frameworks answered correctly — 100% evidence
completeness, zero disagreements with ground truth, both degrading gracefully
when a source errors. CrewAI is not disqualified on quality, and the ADR says so.

It loses on cost, structurally:

| | LangGraph | CrewAI |
|---|---:|---:|
| Model calls per run | 1 | 11 |
| Tokens in per run | 77 | 4,274 |
| Est. cost per run | $0.000693 | $0.018594 |
| Latency p50 vs 140ms ideal-parallel floor | 145.77ms (+4%) | 225.82ms (+61%) |

CrewAI drives tool use *through* the model: each researcher needs a round trip to
decide to call the one tool it was given. For a subflow where all five sources
are always needed, that reasoning is pure overhead — 26.8× the cost for the same
answer.

The criterion that actually settles it is **human approval**. D9 needs a workflow
to pause, persist, and resume in a different process hours later. LangGraph's
`interrupt()` writes the pause to the PostgreSQL checkpointer. CrewAI's
`Task.human_input` calls `_handle_human_feedback` inline in the agent executor —
a synchronous prompt for a human watching a terminal. That is a different
assumption about where the human is, not a configuration gap.

Two rows favour CrewAI and are recorded as such: its built-in `crewai.mcp` client
is more capable out of the box than a from-scratch equivalent, and it ships
`crewai.a2a` support that Phase 9 implemented by hand.

---

## Two measurement bugs, found before the numbers were trusted

Both would have produced a confidently wrong ADR, and both made CrewAI look
*worse* than it is.

**1. The stub model never called a tool.** The first run scored CrewAI at **0%
evidence completeness** — a damning, quotable number that measured my stand-in
model's laziness rather than the framework. CrewAI renders tools into a ReAct
prompt and expects the model to emit an `Action`; a stub that answers immediately
never triggers one. The stub now emulates a competent model: one tool call, then
an answer. A contributing cause was my own tool definition — `_run(*args,
**kwargs)` rendered as an unusable `ForwardRef('Any')` schema, so the agent could
not have called it correctly even if it tried. Both fixed; CrewAI now scores
1.00.

**2. Per-source latencies were below the platform's timer resolution.** Sources
were 8–25ms, and every one returned in the same ~8ms regardless of its nominal
cost. Windows' default timer granularity is ~15.6ms, so concurrent `asyncio.sleep`
calls below it all wake on the same tick. The per-source differences were
fiction, and the "fan-out costs max not sum" claim would have rested on noise.
Latencies were raised above the floor, after which the numbers became
self-consistent: LangGraph lands within 4% of the ideal-parallel floor, and
removing the slowest source correctly makes *both* frameworks faster.

The first version of this benchmark would have read as more decisive, not less.

---

## Maintenance fix: the stale `HEAD_REVISION`

`tests/integration/test_database.py` asserted
`HEAD_REVISION = "0002_foundation_tables"`. The actual head has been
`0006_workflow_executions` since Phase 6 — a test that could only ever fail for
the wrong reason, and only on a machine that could reach PostgreSQL.

Now derived rather than restated:

```python
def alembic_heads() -> list[str]:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return list(ScriptDirectory.from_config(config).get_heads())
```

Verified to resolve to `['0006_workflow_executions']`. Adding a migration can no
longer leave a false expectation behind. The "exactly one head" assertion was not
duplicated here — `tests/unit/test_migration_schema_consistency.py` already makes
it without a database, so a branch is caught on any machine.

---

## Verification

```
ruff check      clean
mypy --strict   clean, 162 files
pytest          413 passed, 146 skipped   (was 386 / 146)
benchmark       runs offline, no API key, no network
```

27 new tests. The scoring code is tested because §10 makes the measurement the
deliverable: completeness scoring, ground-truth checking (including a fabricated
fact that scores 1.00 on completeness and is still caught), degradation
injection, the percentile definition, and the stub-model behaviour whose earlier
bug produced the false 0%.

Telemetry and tracing are disabled by environment variable before `crewai` is
imported — they phone home by default, which would both distort a latency
benchmark and transmit information about a private codebase.

---

## Still outstanding

- 146 integration/e2e tests pending PostgreSQL (`role "custops"` →
  `InvalidPasswordError`) and Chromium.
- Phases 11–14: evaluation platform, observability, security hardening, CI/CD and
  final documentation.
- Streamlit remains deferred.

---

## Rule 23 — what you should be able to explain

1. Why the comparison measures framework overhead rather than end-to-end latency.
2. Why the CrewAI stub had to emulate a tool call, and what the first version
   measured instead.
3. Why the benchmark lives outside `src/custops/`.
4. Why the per-source latencies had to exceed ~16ms on Windows.
5. What the token-count difference actually comes from, structurally.
6. Why both frameworks scored 100% on evidence completeness, and what that does
   and does not prove.
7. Why the decision would be the same even if CrewAI had been faster.
