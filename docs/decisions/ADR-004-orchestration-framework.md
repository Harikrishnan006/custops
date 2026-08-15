# ADR-004: Orchestration framework — LangGraph over CrewAI

- **Status:** Accepted
- **Date:** 2026-08-15
- **Phase:** 10
- **Required by:** BUILD_SPEC §10 and decision D7 ("CrewAI is a measured
  comparison, not a production path… the measurement *is* the deliverable")

## Decision

**LangGraph remains the orchestration framework. CrewAI is not adopted, in whole
or in part.** It stays a dev-only dependency used by the benchmark, and the
existing implementation is unchanged.

The expected outcome was "LangGraph stays". §10 warns that the data decides, so
the interesting question is whether the data actually supports the expectation or
merely fails to contradict it. It supports it — decisively on cost, and for
reasons that turn out to be structural rather than incidental.

## How the measurement was made

The evidence-research subflow — gather subscription, contract, pricing, policy
and support evidence for an account, then synthesise — implemented twice:

- `benchmarks/research_comparison/langgraph_flow.py` — five retrieval nodes with
  no edges between them (so LangGraph schedules them in one superstep), fanning
  in to a synthesis node.
- `benchmarks/research_comparison/crewai_flow.py` — five specialist researcher
  agents, each with a tool for its own source, tasks marked
  `async_execution=True`, feeding a synthesis task via `context`.

Held constant across both: the same five retrieval sources with the same
injected per-source latency, the same five inputs, the same labelled ground
truth, and the same deterministic offline model. The only variable is the
framework.

Reproduce with:

```bash
uv run python -m benchmarks.research_comparison.run
```

Raw output: `benchmarks/results/research_comparison.json` and `.md`.

### What the numbers do and do not mean

**Latency here is framework overhead, not end-to-end latency.** The model is a
deterministic stub, so the seconds a real model would spend are absent from both
sides equally. In production both would be dominated by identical model calls.
Framework overhead is the variable that actually differs, which is why it is
worth isolating — but "CrewAI is 1.5× slower" must not be read as "1.5× slower
to answer a customer".

**Token counts are estimated at 4 characters per token**, applied identically to
both, so the ratio is sound even though the absolute figures are approximate.
Exact character counts are in the JSON so the estimate can be re-derived.

**Two measurement bugs were found and fixed before these numbers were trusted**,
both of which would have produced a confidently wrong ADR:

1. The first stub model answered without ever calling its tool, scoring CrewAI at
   **0% evidence completeness**. That measured my stand-in's laziness, not the
   framework. The stub now emulates a competent model: one tool call, then an
   answer.
2. Source latencies were originally 8–25ms. Every source returned in the same
   ~8ms regardless — Windows' timer granularity is ~15.6ms, so concurrent sleeps
   below it all wake on the same tick. The per-source differences were fiction,
   and the "fan-out costs max not sum" claim would have rested on noise.
   Latencies were raised above the timer floor.

A benchmark that had shipped with either bug would have read as more decisive,
not less.

## Results

5 inputs × 5 repetitions per scenario, Python 3.11.9 on Windows.
Reference points: perfect overlap of the sources costs **140ms**; perfect
serialisation costs **420ms**.

### Healthy

| Metric | LangGraph | CrewAI | Ratio |
|---|---:|---:|---:|
| Latency p50 (ms) | 145.77 | 225.82 | 1.55× |
| Latency p95 (ms) | 158.58 | 304.77 | 1.92× |
| Model calls per run | 1.00 | 11.00 | **11×** |
| Tokens in (per run) | 77 | 4,274 | **55×** |
| Est. cost per run (USD) | 0.000693 | 0.018594 | **26.8×** |
| Evidence completeness | 1.00 | 1.00 | — |
| Runs disagreeing with ground truth | 0 | 0 | — |

### Degraded (the policy source errors)

| Metric | LangGraph | CrewAI | Ratio |
|---|---:|---:|---:|
| Latency p50 (ms) | 95.44 | 187.52 | 1.96× |
| Latency p95 (ms) | 106.96 | 202.84 | 1.90× |
| Model calls per run | 1.00 | 11.00 | 11× |
| Est. cost per run (USD) | 0.000651 | 0.018595 | 28.6× |
| Evidence completeness (vs 4 reachable sources) | 1.00 | 1.00 | — |
| Runs disagreeing with ground truth | 0 | 0 | — |

## What the data says

**1. Both are correct. Neither fabricates.** 100% completeness in both scenarios,
zero disagreements with ground truth, and both degrade gracefully — the four
healthy sources survive when the fifth errors. *CrewAI is not disqualified on
quality.* Any argument for LangGraph has to be made on cost and control, not on
correctness.

**2. LangGraph hits near-ideal parallelism; CrewAI does not.** LangGraph's 145.77ms
against a 140ms floor is 4% overhead — the fan-out is real. CrewAI's 225.82ms
against the same floor is 61% overhead: `async_execution=True` overlaps the
sources partially, but it lands well short of the parallel regime and well short
of serial (420ms). Confirmed by the degraded scenario, where removing the slowest
source drops LangGraph to 95.44ms against a new 100ms floor.

**3. The cost difference is structural, not tunable.** This is the finding that
decides the ADR. CrewAI drives tool use *through the model*: each researcher
needs a round trip to decide to call the single tool it was given, and another to
report. Eleven model calls per run for five deterministic lookups. The LangGraph
version makes **one** — retrieval is ordinary code; the model is consulted only
for synthesis.

That is not a tuning artefact. It is what an agent framework *is*: agents with
goals reasoning about which tools to use. For a workflow where all five sources
are always needed, that reasoning is pure overhead — 55× the input tokens and
26.8× the cost to reach the same answer.

## The criteria beyond the benchmark

The measurement covers §10's five metrics. The framework also has to carry the
rest of this system, and several of these are decided by facts about CrewAI 1.6.1
that were verified against the installed package rather than recalled.

| Criterion | LangGraph (in use) | CrewAI 1.6.1 |
|---|---|---|
| **Orchestration** | Explicit graph; conditional edges; routing is a function I write and test | Agents choose; `Process.sequential` / `hierarchical`. Control is emergent |
| **State / checkpointing** | `AsyncPostgresSaver` — durable, in the same PostgreSQL as the systems of record | `SQLiteFlowPersistence` for Flows; no PostgreSQL backend. Crews have no checkpointer |
| **Human approval / interrupt** | `interrupt()` + `Command(resume=…)`; the run is *suspended to durable storage* and resumes in a different process, hours later | `Task.human_input=True` calls `_handle_human_feedback` **inline in the executor**. The process blocks waiting for console input |
| **MCP integration** | Our own tool layer wraps every call in permission + approval + audit (§8) | `crewai.mcp` (`MCPServerHTTP`, `MCPServerStdio`, tool filters) — genuinely present and capable |
| **A2A integration** | Our client, our contract, our degradation policy (ADR-006) | `crewai.a2a` (`A2AConfig`, auth) — present |
| **Observability** | structlog JSON with `execution_id` bound through every node, tool call and audit row | Rich event bus, plus telemetry/tracing that **phone home by default** and had to be disabled by env var for this benchmark |
| **Testing** | Nodes are plain async functions; the graph is compiled and invoked in-process; deterministic providers make every path reproducible | Requires standing up agents and a crew; behaviour routed through model decisions is harder to pin |

**The decisive row is human approval.** D9 requires a workflow to pause on an
approval, persist, and resume — possibly in another process, possibly the next
day, with the approval independently verified at three layers. LangGraph's
`interrupt()` writes the pause to the checkpointer; the API resumes it via
`WorkflowRunner.resume()`. CrewAI's human-input mechanism is a synchronous
prompt inside the agent executor. It is designed for a human sitting at a
terminal watching a crew run, not for an approval that arrives through an HTTP
endpoint after the original process has exited.

That is not a gap CrewAI could close with configuration. It is a different
assumption about where the human is.

**Two rows favour CrewAI and should be recorded honestly.** Its built-in MCP
client is more capable out of the box than anything we would write from scratch,
and it ships A2A support we implemented by hand in Phase 9. If this project were
starting today with no tool layer, those would be real arguments. They do not
apply here: our MCP layer exists because permission, approval and audit must be
enforced at the tool boundary (§8, D9), and a client that simply calls tools
would not satisfy that requirement whoever wrote it.

## Consequences

- The existing LangGraph implementation is untouched. No production code changed
  in this phase.
- CrewAI stays a **dev-only** dependency. `benchmarks/` sits outside
  `src/custops/`, so nothing shipped can import it.
- The benchmark is re-runnable and its scoring is unit-tested
  (`tests/unit/test_research_comparison_*.py`), because §10 makes the measurement
  a deliverable and a scoring bug would produce a confident, wrong conclusion.
- **What would reopen this.** If a future workflow needed genuinely open-ended
  research — where which sources to consult is not known in advance and a model
  choosing them is the point — the 11-calls-per-run overhead would stop being
  waste and start being the feature. Subscription Upgrade is the opposite: five
  known sources, every time.

## Alternatives considered

**Adopt CrewAI for the research subflow only, keeping LangGraph elsewhere.**
Rejected. It would add a second orchestration model, a second failure surface and
a second thing to explain, in exchange for 26.8× the cost on the one subflow
where the source list is fixed and known.

**Run the comparison on paper.** Cheaper, and it would have reached the same
conclusion. Rejected because §10 asks for a measurement, and because the two
measurement bugs found here are exactly what a paper comparison cannot catch — I
would have asserted the fan-out advantage without noticing the platform was not
measuring it.

**Declare CrewAI unsuitable on quality.** Unsupported by the data, and the first
draft of this ADR could have said it: the initial run showed 0% completeness.
That number was my stub's fault. Reporting it would have been the easy,
comfortable result and it would have been false.
