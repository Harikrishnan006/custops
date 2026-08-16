# Phase 11 — completion report

**Date:** 2026-08-16
**Scope:** Evaluation — AgentForge import, CustOps adapter, §15 datasets, regression gate
**Status:** Code complete. Datasets, adapter, evaluators and the CI gate verified
without infrastructure; adapter-against-a-real-execution pending PostgreSQL.

---

## 1. AgentForge version resolved

```
agent-forge==0.1.0 (from git+https://github.com/Harikrishnan006/agent-forge@3b85de91a57d0c75e110e985b52ed9232919f656)
```

Tag `v0.1.0` → commit `3b85de9`, pinned in `pyproject.toml` under
`[tool.uv.sources]`. **Dev dependency group**, never a production dependency.

## 2. Public APIs used

Everything scoring-related is called, nothing reimplemented:

| Purpose | AgentForge API |
|---|---|
| Score traces | `agent_eval.evaluate_traces`, `agent_eval.score_single_trace` |
| Roll up metrics | `metrics.summarise_trace_scores` |
| Load datasets | `agent_eval.load_golden_tasks` |
| Failure breakdown | `agent_eval.failure_mode_breakdown` |
| Run identity/storage | `storage.new_run_id`, `storage.save_run` |
| **The gate** | `regression.compare_runs` |
| Types | `models.{AgentTrace, TraceStep, StepType, GoldenTask, TraceScore, EvalRun}` |

## 3. Gated vs report-only metrics

You asked me to check for a supported extension mechanism first. **There is
none in v0.1.0**: `compare_runs(baseline, current)` takes no rules argument,
`config` exposes only `judge_available()` and `runs_dir()` as functions,
`REGRESSION_RULES` is a plain module dict with no registration API, and
`_classify` is private. So per your instruction the global dict is **not**
mutated and **no second comparator** was written.

**Gated (6)** — these fail a build:
`task_success_rate`, `tool_correctness`, `tool_hallucination_rate`,
`escalation_accuracy`, `avg_steps`, `avg_cost_usd`.

**Report-only (11)** — surfaced, never gating:
`avg_step_efficiency`, `avg_latency_ms`, `total_tasks`, `passed`, and the nine
`custops_*` metrics (workflow completion rate, escalation rate, planning
accuracy, retrieval precision, retrieval recall, retry rate, replan rate,
approval rejections, validation accuracy).

A test asserts every metric claimed as gated is genuinely in
`config.REGRESSION_RULES`, and another asserts that degrading a `custops_*`
metric does **not** trip the gate — so the boundary is documented by executable
fact rather than by this paragraph. **Extending the rules belongs in AgentForge**,
as a future change there.

Note `avg_steps` is absent from `summarise_trace_scores` and added by the runner
the same way `run_agent_eval` does. Omitting it would have silently narrowed the
gate from six metrics to five.

## 4. Datasets

**13 scenarios / 13 golden tasks**, one-to-one — a trace with no matching task
is silently skipped by AgentForge, which would shrink the evaluation without
anything failing.

All **11 adversarial cases §15 names**: customer not found, inactive account,
contract restriction, discount above threshold, billing API timeout, CRM update
failure, entitlement/billing divergence, malformed contract document, low
retrieval confidence, approval rejection, browser failure. Plus **2 that should
succeed** — a set of only failures cannot detect a platform that refuses
everything.

`available_tools` is read from the **live permission matrix**, not restated in
the dataset: hallucination detection compares against it, so a hand-copied
inventory would drift and start reporting real tools as hallucinations.

## 5. Tests

| Suite | Passing | Pending |
|---|---:|---:|
| `test_evaluation_adapter.py` | 30 | — |
| `test_evaluation_gate.py` | 24 | — |
| `test_evaluation_isolation.py` | 5 | — |
| `test_evaluation_real_trace.py` | — | 4 |
| **Total added** | **59** | **4** |

```
Phase 12 baseline (2f87a4c):  490 passed, 156 skipped
Phase 11:                     549 passed, 160 skipped
```

Measured with `git stash`, not recalled. No existing test weakened or removed.

The adapter suite covers all six step types and — with particular care — the
**REFUSAL/ESCALATION split**, including a parametrised check across all 13
scenarios. AgentForge cannot catch a confusion between the two (both make
`AgentTrace.escalated` true), so the distinction is only ever as good as these
tests.

## 6. CI gate behaviour

`.github/workflows/evaluation.yml` runs `custops evaluate` against the committed
baseline at `evaluation/baseline/trace_baseline.json`. The judge is never used —
a gate that fails because an API call timed out is worse than no gate.

Proven, not asserted:

- **Equal baseline → exit 0.** All six gated metrics `[=]`, "PASSED: no
  regressions detected".
- **Planted regression → exit 1.** A doctored baseline makes the current run
  regress; `run_cli` returns 1. A second test degrades four gated metrics
  directly and requires `compare_runs` to catch every one.
- **Missing baseline → exit 0.** A first run on a new branch has nothing to
  compare against; treating that as a regression would block every new branch.

## 7. Phase 12 unchanged

**Yes — zero modifications.** `git diff HEAD` across
`observability/`, `apps/api/routers/workflows.py`, `apps/api/schemas/workflow.py`,
`mcp/tools/runtime.py`, `agents/nodes.py` and `apps/orchestrator/runner.py`
returns empty. The adapter consumes the Phase 12 trace exactly as delivered and
found no missing field — the `decision_made`, `approval_received` and
`validation_completed` payloads carry precisely what the REFUSAL/ESCALATION
split needs.

Only `pyproject.toml` (dependency), `src/custops/cli.py` (an `evaluate`
subcommand, imported lazily) and `uv.lock` changed outside the new package.

## 8. Isolation

`agent-forge` is absent from `[project.dependencies]`; no production module
imports `custops.evaluation` or `agent_forge` at module level; the CLI imports
the evaluation command *inside* its handler so `custops seed` works on an
install that has neither. Tests assert all four.

---

## Known limitation, stated plainly

`agent-forge@v0.1.0` ships no `py.typed` marker, so mypy will not read its
annotations despite the source being fully typed. CustOps carries a
`module = ["agent_forge.*"]` override — the same accommodation already made for
asyncpg. **This is a packaging gap in AgentForge worth fixing there**, not
working around further here. No production module loses type coverage, since the
evaluation layer is dev-only.

## Infrastructure-dependent verification (4 tests, pending)

`tests/integration/test_evaluation_real_trace.py` — **PostgreSQL unusable: role
`custops` does not exist (`InvalidPasswordError`)**. These run a genuine
Subscription Upgrade, read the trace back out of PostgreSQL, and put it through
the same adapter and the same AgentForge scoring the gate uses. They are what
would catch a Phase 12 field the adapter depends on being renamed — something
the synthetic suite structurally cannot see.

Unchanged and still pending from earlier phases: full workflow traces, the A2A
subprocess pair (**PostgreSQL**), provisioning (**Chromium**).

## Still outstanding

- Phases 13–14: security hardening; CI/CD and final documentation.
- 160 tests pending PostgreSQL / Chromium.

---

## Rule 23 — what you should be able to explain

1. Why AgentForge is a dev dependency and not a runtime one.
2. How a CustOps execution becomes an `AgentTrace`, step type by step type.
3. The difference between REFUSAL and ESCALATION, and why AgentForge cannot
   catch a confusion between them.
4. Why the datasets hold CustOps rows rather than ready-made traces.
5. Why `available_tools` is read from the permission matrix.
6. Which metrics gate a build and which are report-only, and why that split
   exists rather than being a preference.
7. Why `avg_steps` had to be added to the summary by hand.
8. What a REASONING step is allowed to contain, and why.
