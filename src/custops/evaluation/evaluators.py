"""Orchestrator-specific metrics (§15) — and only those.

Everything a generic harness can compute is AgentForge's job. This module is
deliberately small, and its boundary is worth stating precisely:

**AgentForge already computes** task success, tool selection accuracy
(``tool_sequence_correctness``), tool-call correctness, tool hallucination, step
efficiency, escalation correctness, cost and latency. None of that is
reimplemented here.

**Only these are left**, because each depends on knowledge of *this*
orchestrator that no generic harness could have:

* **workflow completion rate** — CustOps has terminal states AgentForge cannot
  interpret. A run that escalated is not a failed run.
* **planning accuracy** — needs a ground-truth plan per case.
* **retrieval precision / recall** — needs a labelled evidence set.
* **validation accuracy** — did the Validator catch an *injected* divergence?
  Only the dataset knows a divergence was planted.
* **retry / escalation rate** — CustOps budget concepts.

A metric that could be computed from an ``AgentTrace`` alone does not belong
here.
"""

from __future__ import annotations

from dataclasses import dataclass

from custops.agents.state import WorkflowStatus
from custops.evaluation.datasets.scenarios import LabelledScenario
from custops.observability.events import EventType


@dataclass(frozen=True, slots=True)
class RetrievalQuality:
    """Precision and recall of gathered evidence against the labelled set."""

    precision: float
    recall: float


def evidence_sources(scenario: LabelledScenario) -> frozenset[str]:
    """Which evidence sources the run actually reached.

    Derived from the tools it called rather than from the evidence list, because
    a source the workflow *claims* it consulted without calling the tool is
    exactly the failure worth catching.
    """
    mapping = {
        "get_customer": "account",
        "get_subscription": "subscription",
        "get_invoice": "invoice",
        "get_support_history": "support",
        "get_contract": "contract",
        "search_knowledge": "policy",
    }
    reached: set[str] = set()
    for call in scenario.record.tool_calls:
        source = mapping.get(call.tool_name)
        if source is not None and call.succeeded:
            reached.add(source)
    return frozenset(reached)


def retrieval_quality(scenario: LabelledScenario) -> RetrievalQuality:
    """Precision and recall against the labelled evidence set (§15).

    Both, not one: recall alone rewards gathering everything indiscriminately,
    and precision alone rewards gathering almost nothing.
    """
    expected = scenario.expected_evidence
    reached = evidence_sources(scenario)

    if not expected and not reached:
        return RetrievalQuality(precision=1.0, recall=1.0)

    hits = len(reached & expected)
    precision = hits / len(reached) if reached else 0.0
    recall = hits / len(expected) if expected else 1.0
    return RetrievalQuality(precision=round(precision, 3), recall=round(recall, 3))


def planning_accuracy(scenario: LabelledScenario) -> float:
    """Fraction of the ground-truth plan the run actually visited, in order.

    Order matters: validating before executing is not the same plan carried out
    badly, it is a different plan.
    """
    expected = scenario.expected_plan
    if not expected:
        return 1.0

    visited = [step.node for step in sorted(scenario.record.steps, key=lambda s: s.sequence)]
    position = 0
    matched = 0
    for node in expected:
        while position < len(visited) and visited[position] != node:
            position += 1
        if position < len(visited):
            matched += 1
            position += 1

    return round(matched / len(expected), 3)


def completed(scenario: LabelledScenario) -> bool:
    return str(scenario.record.execution.status) == WorkflowStatus.COMPLETED


def escalated(scenario: LabelledScenario) -> bool:
    return str(scenario.record.execution.status) in {
        WorkflowStatus.ESCALATED,
        WorkflowStatus.FAILED,
    }


def validation_accuracy(scenarios: list[LabelledScenario]) -> float | None:
    """Did the Validator catch the divergences that were injected? (§14, §15)

    ``None`` when no scenario injected one — reporting 1.0 for a set that never
    tested the Validator would be a claim the data does not support.
    """
    injected = [s for s in scenarios if s.divergence_injected]
    if not injected:
        return None
    return round(sum(1 for s in injected if s.divergence_caught) / len(injected), 3)


def retry_rate(scenarios: list[LabelledScenario]) -> float:
    if not scenarios:
        return 0.0
    retried = sum(1 for s in scenarios if s.record.execution.retry_count > 0)
    return round(retried / len(scenarios), 3)


def replan_rate(scenarios: list[LabelledScenario]) -> float:
    if not scenarios:
        return 0.0
    replanned = sum(1 for s in scenarios if s.record.execution.replan_count > 0)
    return round(replanned / len(scenarios), 3)


def approval_rejection_count(scenarios: list[LabelledScenario]) -> int:
    """How many runs were stopped by a human. Reported, never gated —
    a reviewer declining more upgrades is not a platform regression."""
    total = 0
    for scenario in scenarios:
        for event in scenario.record.events:
            if event.event_type == str(EventType.APPROVAL_RECEIVED) and (
                event.payload or {}
            ).get("approved") is False:
                total += 1
    return total


def custops_metrics(scenarios: list[LabelledScenario]) -> dict[str, float]:
    """The §15 orchestrator metrics, as a summary block.

    Merged into the ``EvalRun`` summary alongside AgentForge's own. They are
    **report-only**: ``compare_runs`` gates on the metrics named in
    AgentForge's ``REGRESSION_RULES``, and v0.1.0 exposes no public way to
    extend that table. Adding one belongs in AgentForge, not around it here.
    """
    if not scenarios:
        return {}

    total = len(scenarios)
    precisions = [retrieval_quality(s).precision for s in scenarios]
    recalls = [retrieval_quality(s).recall for s in scenarios]

    metrics: dict[str, float] = {
        "custops_workflow_completion_rate": round(
            sum(1 for s in scenarios if completed(s)) / total, 3
        ),
        "custops_escalation_rate": round(sum(1 for s in scenarios if escalated(s)) / total, 3),
        "custops_planning_accuracy": round(
            sum(planning_accuracy(s) for s in scenarios) / total, 3
        ),
        "custops_retrieval_precision": round(sum(precisions) / total, 3),
        "custops_retrieval_recall": round(sum(recalls) / total, 3),
        "custops_retry_rate": retry_rate(scenarios),
        "custops_replan_rate": replan_rate(scenarios),
        "custops_approval_rejections": float(approval_rejection_count(scenarios)),
    }

    accuracy = validation_accuracy(scenarios)
    if accuracy is not None:
        metrics["custops_validation_accuracy"] = accuracy

    return metrics
