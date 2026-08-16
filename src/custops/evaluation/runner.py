"""Running an evaluation pass and checking it for regressions (§15).

The division of labour is the point of this module, so it is worth stating in
one place. CustOps supplies **inputs and orchestrator-specific context**;
AgentForge does **all** the scoring and the gate:

===================================  ==================================
CustOps                              AgentForge (``agent-forge@v0.1.0``)
===================================  ==================================
adapter → ``AgentTrace``             ``evaluate_traces``
golden/adversarial datasets          ``load_golden_tasks``
§15 orchestrator metrics             ``summarise_trace_scores``
—                                    ``EvalRun`` / ``storage.save_run``
—                                    ``compare_runs`` (the gate)
===================================  ==================================

**Gated vs report-only.** ``compare_runs`` compares only metrics named in
AgentForge's ``config.REGRESSION_RULES``. v0.1.0 exposes no public API to supply
or extend that table — ``compare_runs`` takes no rules argument and ``config``
offers no registration function. Rather than mutate another package's module
state or write a second comparator, CustOps metrics are reported and
AgentForge's are gated. Extending the rules belongs in AgentForge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_forge import storage
from agent_forge.agent_eval import evaluate_traces, failure_mode_breakdown, load_golden_tasks
from agent_forge.metrics import summarise_trace_scores
from agent_forge.models import AgentTrace, EvalRun, GoldenTask, TraceScore
from agent_forge.regression import RegressionReport, compare_runs

from custops.evaluation.adapter import to_agent_trace
from custops.evaluation.datasets.scenarios import SCENARIOS, LabelledScenario
from custops.evaluation.evaluators import custops_metrics
from custops.mcp.permissions.matrix import Role, tools_for_role

DATASET_PATH = Path(__file__).parent / "datasets" / "golden_tasks.json"

EVAL_TYPE = "trace"

# Metrics AgentForge's gate actually compares, for a trace-level run: the
# intersection of what `summarise_trace_scores` produces with what
# `config.REGRESSION_RULES` names. Stated here so the completion report and the
# CLI can be honest about which numbers can fail a build.
GATED_METRICS = frozenset(
    {
        "task_success_rate",
        "tool_correctness",
        "tool_hallucination_rate",
        "escalation_accuracy",
        "avg_steps",
        "avg_cost_usd",
    }
)


def available_tools() -> list[str]:
    """Every tool an agent in this platform may call.

    Read from the live permission matrix rather than restated in the dataset:
    tool-hallucination detection compares against this list, so a hand-copied
    version would drift and start calling real tools hallucinations.
    """
    names: set[str] = set()
    for role in (Role.SUPERVISOR, Role.RESEARCH, Role.EXECUTION, Role.VALIDATOR, Role.PLANNER):
        names.update(tools_for_role(role))
    return sorted(names)


def load_tasks(path: Path | None = None) -> list[GoldenTask]:
    """Golden tasks, loaded by AgentForge, with the tool inventory filled in."""
    tasks: list[GoldenTask] = list(load_golden_tasks(path or DATASET_PATH))
    inventory = available_tools()
    for task in tasks:
        if not task.available_tools:
            task.available_tools = list(inventory)
    return tasks


def build_traces(scenarios: list[LabelledScenario] | None = None) -> list[AgentTrace]:
    """Run every scenario through the real adapter.

    The adapter is exercised by every evaluation rather than bypassed — which is
    why the dataset holds CustOps rows and not ready-made traces.
    """
    return [
        to_agent_trace(scenario.record, task_id=scenario.task_id)
        for scenario in (scenarios if scenarios is not None else SCENARIOS)
    ]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """One evaluation pass, plus the regression verdict if one was requested."""

    run: EvalRun
    scores: list[TraceScore]
    report: RegressionReport | None

    @property
    def has_regression(self) -> bool:
        return self.report is not None and self.report.has_regression

    @property
    def gated_metrics(self) -> dict[str, Any]:
        return {k: v for k, v in self.run.summary.items() if k in GATED_METRICS}

    @property
    def report_only_metrics(self) -> dict[str, Any]:
        return {k: v for k, v in self.run.summary.items() if k not in GATED_METRICS}


def evaluate(
    *,
    version: str,
    scenarios: list[LabelledScenario] | None = None,
    tasks_path: Path | None = None,
    persist: bool = False,
    notes: str = "",
) -> tuple[EvalRun, list[TraceScore]]:
    """Score every scenario. All scoring is AgentForge's."""
    cases = scenarios if scenarios is not None else SCENARIOS
    tasks = load_tasks(tasks_path)
    traces = build_traces(cases)

    scores = evaluate_traces(traces, tasks, use_judge=False)

    summary: dict[str, Any] = dict(summarise_trace_scores(scores))
    # `summarise_trace_scores` omits avg_steps; `run_agent_eval` adds it the same
    # way. It is a *gated* metric, so leaving it out would silently narrow the
    # gate rather than merely thin the report.
    summary["avg_steps"] = (
        round(sum(t.step_count for t in traces) / len(traces), 2) if traces else 0.0
    )
    summary.update(custops_metrics(cases))

    run = EvalRun(
        run_id=storage.new_run_id(version, EVAL_TYPE),
        version=version,
        eval_type=EVAL_TYPE,
        summary=summary,
        records=[score.to_dict() for score in scores],
        notes=notes,
    )

    if persist:
        storage.save_run(run)

    return run, scores


def compare_with_baseline(current: EvalRun, baseline: EvalRun) -> RegressionReport:
    """The gate. AgentForge's, unmodified."""
    return compare_runs(baseline, current)


def load_baseline(path: Path) -> EvalRun:
    """Read a committed baseline run from disk.

    Deliberately a file rather than AgentForge's run store: CI needs a baseline
    that is reviewed and versioned with the code, not whatever happened to run
    last on some machine.
    """
    return EvalRun.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_baseline(run: EvalRun, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.to_dict(), indent=2) + "\n", encoding="utf-8")


def failure_modes(scores: list[TraceScore]) -> dict[str, int]:
    """AgentForge's breakdown, surfaced for the CLI."""
    breakdown: dict[str, int] = dict(failure_mode_breakdown(scores))
    return breakdown
