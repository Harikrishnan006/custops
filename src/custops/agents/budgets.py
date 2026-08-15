"""Retry and replan budgets.

BUILD_SPEC §7: *"Retry and replan budgets are configuration values enforced in
Python, not LLM decisions."*

The reason is specific. A model asked "should I try again?" after a failure will
usually say yes — it has no cost model, no memory of how many times it already
has, and every incentive to appear helpful. Unbounded retry against a mutating
tool is how one workflow becomes four charges. So the budget lives here, the
count lives in state, and the graph's edge consults this module rather than a
model.

A second rule: **only transient failures consume a retry.** Retrying a
permission denial or a "customer not found" cannot succeed and merely burns the
budget that a genuinely transient failure would have needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BudgetOutcome(StrEnum):
    """What the run is allowed to do next."""

    RETRY = "retry"
    REPLAN = "replan"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """How much failure a single run may absorb.

    Deliberately small. These are *bounded* recovery attempts, not resilience:
    a workflow that needs five replans to succeed has a problem no amount of
    retrying will fix, and a human should see it.
    """

    max_retries: int = 2
    max_replans: int = 1


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    outcome: BudgetOutcome
    reason: str


def next_recovery_step(
    *,
    retry_count: int,
    replan_count: int,
    failure_is_retryable: bool,
    policy: BudgetPolicy | None = None,
) -> BudgetDecision:
    """Decide whether a failed run may retry, replan, or must escalate.

    Order is deliberate: retry the same plan first (cheapest, most likely to fix
    a transient fault), then replan (the plan itself may be wrong), then
    escalate. A non-retryable failure skips straight past retry — trying the
    identical call again cannot change a permission denial.
    """
    active = policy if policy is not None else BudgetPolicy()

    if failure_is_retryable and retry_count < active.max_retries:
        return BudgetDecision(
            BudgetOutcome.RETRY,
            f"Transient failure; retry {retry_count + 1} of {active.max_retries}.",
        )

    if replan_count < active.max_replans:
        reason = (
            f"Retry budget exhausted ({retry_count}/{active.max_retries}); "
            f"replan {replan_count + 1} of {active.max_replans}."
            if failure_is_retryable
            else (f"Failure is not retryable; replan {replan_count + 1} of {active.max_replans}.")
        )
        return BudgetDecision(BudgetOutcome.REPLAN, reason)

    return BudgetDecision(
        BudgetOutcome.ESCALATE,
        (
            f"Budgets exhausted (retries {retry_count}/{active.max_retries}, "
            f"replans {replan_count}/{active.max_replans}); escalating to a human."
        ),
    )
