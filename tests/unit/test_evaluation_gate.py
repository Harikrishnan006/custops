"""The evaluation run and the regression gate (§15).

Two things are proven here that matter more than the metric values themselves:

* the gate **fails a build** when AgentForge's rules detect a regression —
  demonstrated with a deliberately planted one, not asserted;
* the datasets, the adapter and AgentForge's scoring actually fit together end
  to end, over the real §15 cases rather than a toy.

Nothing in this file reimplements scoring or comparison. ``compare_runs`` is
AgentForge's, called unmodified.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_forge.models import EvalRun

from custops.evaluation.cli import run as run_cli
from custops.evaluation.datasets.scenarios import SCENARIOS
from custops.evaluation.runner import (
    GATED_METRICS,
    compare_with_baseline,
    evaluate,
    load_baseline,
    load_tasks,
    write_baseline,
)

BASELINE_PATH = (
    Path(__file__).resolve().parents[2] / "evaluation" / "baseline" / "trace_baseline.json"
)


# --------------------------------------------------------------- the datasets


def test_every_scenario_has_a_matching_golden_task() -> None:
    """A trace with no task is silently skipped by AgentForge.

    That would shrink the evaluation set without anything failing — the worst
    kind of gap, because the gate would still report success.
    """
    task_ids = {task.task_id for task in load_tasks()}
    scenario_ids = {scenario.task_id for scenario in SCENARIOS}

    assert scenario_ids == task_ids


def test_the_dataset_covers_every_adversarial_case_the_spec_names() -> None:
    """§15 lists these explicitly. Missing one is a spec gap, not a preference."""
    required = {
        "customer-not-found",
        "inactive-account",
        "contract-restriction",
        "discount-above-threshold",
        "billing-api-timeout",
        "crm-update-failure",
        "entitlement-billing-divergence",
        "malformed-contract-document",
        "low-retrieval-confidence",
        "approval-rejection",
        "browser-failure",
    }

    assert required <= {scenario.task_id for scenario in SCENARIOS}


def test_the_dataset_includes_cases_that_should_succeed() -> None:
    """A set of only failures cannot detect a platform that refuses everything."""
    tasks = {task.task_id: task for task in load_tasks()}
    succeeding = [task for task in tasks.values() if not task.should_refuse]

    assert len(succeeding) >= 2


def test_available_tools_come_from_the_live_permission_matrix() -> None:
    """Hallucination detection compares against this list.

    A hand-copied inventory would drift from the matrix and start reporting
    real tools as hallucinations — or worse, stop reporting invented ones.
    """
    from custops.mcp.permissions.matrix import ToolName

    tasks = load_tasks()
    inventory = set(tasks[0].available_tools)

    assert inventory
    assert inventory <= {str(tool) for tool in ToolName}
    assert str(ToolName.UPDATE_SUBSCRIPTION) in inventory


def test_no_expected_tool_is_outside_the_permission_matrix() -> None:
    """An expected tool that cannot exist would be unreachable by any run."""
    from custops.mcp.permissions.matrix import ToolName

    known = {str(tool) for tool in ToolName}
    for task in load_tasks():
        unknown = set(task.expected_tools) - known
        assert not unknown, f"{task.task_id} expects unknown tools: {sorted(unknown)}"


# ------------------------------------------------------------- the eval pass


def test_the_full_dataset_scores_end_to_end() -> None:
    run, scores = evaluate(version="test")

    assert len(scores) == len(SCENARIOS)
    assert run.summary["total_tasks"] == len(SCENARIOS)


def test_the_summary_carries_both_gated_and_report_only_metrics() -> None:
    run, _ = evaluate(version="test")

    assert set(run.summary) >= GATED_METRICS
    assert any(key.startswith("custops_") for key in run.summary)


def test_avg_steps_is_present_because_the_gate_compares_it() -> None:
    """``summarise_trace_scores`` omits it; leaving it out would silently
    narrow the gate from six metrics to five."""
    run, _ = evaluate(version="test")

    assert "avg_steps" in run.summary


def test_custops_metrics_are_namespaced_so_they_cannot_collide() -> None:
    """AgentForge owns the unprefixed metric names in its rules table."""
    run, _ = evaluate(version="test")

    custops_keys = {key for key in run.summary if key.startswith("custops_")}
    assert custops_keys
    assert not (custops_keys & GATED_METRICS)


# ------------------------------------------------------------------ the gate


def test_an_identical_run_does_not_trip_the_gate() -> None:
    run, _ = evaluate(version="v1")
    same, _ = evaluate(version="v2")

    report = compare_with_baseline(same, run)

    assert not report.has_regression


def test_a_planted_regression_trips_the_gate() -> None:
    """The behaviour the CI gate exists for, demonstrated rather than asserted.

    Degrades exactly the metrics AgentForge's rules name, then requires the
    report to catch every one of them.
    """
    baseline, _ = evaluate(version="baseline")

    degraded_summary = dict(baseline.summary)
    degraded_summary["task_success_rate"] = 0.4
    degraded_summary["tool_correctness"] = 0.3
    degraded_summary["tool_hallucination_rate"] = 0.5
    degraded_summary["escalation_accuracy"] = 0.2
    degraded = EvalRun(
        run_id="degraded", version="bad", eval_type="trace", summary=degraded_summary
    )

    report = compare_with_baseline(degraded, baseline)

    assert report.has_regression
    regressed = {delta.metric for delta in report.regressions}
    assert {
        "task_success_rate",
        "tool_correctness",
        "tool_hallucination_rate",
        "escalation_accuracy",
    } <= regressed


def test_a_degraded_custops_metric_does_not_trip_the_gate() -> None:
    """Documents the boundary honestly.

    CustOps metrics are report-only because AgentForge v0.1.0 exposes no public
    way to supply regression rules. If this test ever starts failing, AgentForge
    has grown that API and the metrics can be gated properly.
    """
    baseline, _ = evaluate(version="baseline")

    degraded_summary = dict(baseline.summary)
    degraded_summary["custops_planning_accuracy"] = 0.0
    degraded_summary["custops_retrieval_recall"] = 0.0
    degraded = EvalRun(
        run_id="degraded", version="bad", eval_type="trace", summary=degraded_summary
    )

    report = compare_with_baseline(degraded, baseline)

    assert not report.has_regression


# ------------------------------------------------------------------- the CLI


def test_the_committed_baseline_is_loadable_and_current() -> None:
    """A baseline that no longer matches the datasets would gate on nothing."""
    assert BASELINE_PATH.is_file(), "no committed baseline to gate against"
    baseline = load_baseline(BASELINE_PATH)

    assert baseline.eval_type == "trace"
    assert set(baseline.summary) >= GATED_METRICS
    assert baseline.summary["total_tasks"] == len(SCENARIOS)


def test_the_cli_exits_zero_against_the_committed_baseline() -> None:
    exit_code = run_cli(["--version", "test", "--baseline", str(BASELINE_PATH)])

    assert exit_code == 0


def test_the_cli_exits_non_zero_against_a_planted_regression(tmp_path: Path) -> None:
    """End-to-end proof that a quality drop fails the build.

    The baseline is doctored to be better than achievable, so the current run
    regresses against it and the process must exit non-zero.
    """
    pristine = load_baseline(BASELINE_PATH)
    summary = dict(pristine.summary)
    summary["task_success_rate"] = 1.0
    summary["tool_correctness"] = 1.0
    summary["escalation_accuracy"] = 1.0
    summary["avg_steps"] = 1.0  # far below anything the real dataset produces
    doctored = tmp_path / "pristine.json"
    doctored.write_text(
        json.dumps(
            EvalRun(
                run_id="pristine", version="pristine", eval_type="trace", summary=summary
            ).to_dict()
        ),
        encoding="utf-8",
    )

    exit_code = run_cli(["--version", "current", "--baseline", str(doctored)])

    assert exit_code == 1


def test_the_cli_writes_a_baseline_when_asked(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "baseline.json"

    exit_code = run_cli(["--version", "fresh", "--baseline", str(target), "--write-baseline"])

    assert exit_code == 0
    assert target.is_file()
    assert load_baseline(target).summary["total_tasks"] == len(SCENARIOS)


def test_a_missing_baseline_does_not_fail_the_build(tmp_path: Path) -> None:
    """The first run on a new branch has nothing to compare against. That is
    not a regression, and treating it as one would block every new repo."""
    exit_code = run_cli(["--version", "first", "--baseline", str(tmp_path / "absent.json")])

    assert exit_code == 0


def test_writing_a_baseline_round_trips(tmp_path: Path) -> None:
    run, _ = evaluate(version="roundtrip")
    path = tmp_path / "b.json"

    write_baseline(run, path)

    assert load_baseline(path).summary == run.summary


@pytest.mark.parametrize("metric", sorted(GATED_METRICS))
def test_each_gated_metric_is_actually_in_agentforge_rules(metric: str) -> None:
    """Guards the claim in the completion report.

    If AgentForge changes its rules table, a metric CustOps documents as gated
    could silently stop gating.
    """
    from agent_forge import config

    assert metric in config.REGRESSION_RULES
