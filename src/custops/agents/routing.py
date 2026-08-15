"""Conditional-edge routing (BUILD_SPEC §7 topology).

Every function here is a **pure function of state**. That is the whole design:
LangGraph calls them to pick the next node, they consult only the state and the
budget policy, and none of them asks a model anything.

The topology from §7:

    supervisor -> planner -> research -> decide
                                          |- requires_approval -> approval_gate -> execute
                                          \\- auto                                -> execute
    execute -> validate
    validate |- PASS                       -> notify -> complete
             |- FAIL & retry budget left   -> execute
             |- FAIL & replan budget left  -> planner
             \\- FAIL & budgets exhausted  -> escalate
    research |- evidence_sufficient        -> decide
             \\- low retrieval confidence  -> escalate

Because these are ordinary functions over an ordinary dict, the entire routing
behaviour of the graph is unit-testable with no LangGraph runtime, no database
and no model — which is where most of this phase's verification value sits.
"""

from __future__ import annotations

from custops.agents.budgets import BudgetOutcome, BudgetPolicy, next_recovery_step
from custops.agents.state import (
    ApprovalState,
    ValidationVerdict,
    WorkflowState,
    WorkflowType,
)

# Node names. Constants rather than string literals so a typo is an
# AttributeError at import rather than an edge that silently never fires.
SUPERVISOR = "supervisor"
PLANNER = "planner"
RESEARCH = "research"
DECIDE = "decide"
APPROVAL_GATE = "approval_gate"
EXECUTE = "execute"
VALIDATE = "validate"
NOTIFY = "notify"
COMPLETE = "complete"
ESCALATE = "escalate"


def route_after_supervisor(state: WorkflowState) -> str:
    """Classified requests plan; unclassifiable ones escalate.

    An unknown workflow type must never fall through into the one workflow that
    happens to exist — that turns "I don't understand this request" into
    "upgrading your subscription".
    """
    if state.get("workflow_type") == WorkflowType.SUBSCRIPTION_UPGRADE:
        return PLANNER
    return ESCALATE


def route_after_research(state: WorkflowState) -> str:
    """Sufficient evidence decides; thin evidence escalates.

    The sufficiency verdict is computed by ``domain.policies.retrieval`` from
    similarity scores alone and stored on the state by the research node. This
    edge only reads it — deciding "do I know enough?" is exactly the judgement a
    model should not make about its own work.
    """
    evidence = state.get("evidence") or []
    if not evidence:
        return ESCALATE

    if state.get("metadata", {}).get("evidence_sufficient") is True:
        return DECIDE
    return ESCALATE


def route_after_decide(state: WorkflowState) -> str:
    """Approval-requiring actions stop at the gate; the rest proceed.

    Note what is *not* here: no branch lets a high-confidence decision skip the
    gate. Confidence is an input to whether approval is required (§13), never a
    reason to bypass it.
    """
    if state.get("approval_status") == ApprovalState.REQUIRED:
        return APPROVAL_GATE
    return EXECUTE


def route_after_approval(state: WorkflowState) -> str:
    """Only an explicit grant proceeds.

    Anything else — rejected, still pending, missing — escalates. This mirrors
    the tool layer's exact-match rule (D9): "not rejected" is not "approved".
    Even if this edge were wrong, the MCP tool would still refuse; that is the
    point of enforcing in both places.
    """
    if state.get("approval_status") == ApprovalState.GRANTED:
        return EXECUTE
    return ESCALATE


def route_after_validate(state: WorkflowState, policy: BudgetPolicy | None = None) -> str:
    """PASS completes; FAIL consults the budget; NEEDS_REVIEW escalates.

    ``NEEDS_REVIEW`` is deliberately not retried. It means the Validator could
    not tell whether the outcome was correct, and repeating an action whose
    effect is unknown is how a single upgrade becomes two.
    """
    results = state.get("validation_results") or []
    if not results:
        return ESCALATE

    verdicts = {result["verdict"] for result in results}

    if verdicts == {ValidationVerdict.PASS}:
        return NOTIFY

    if ValidationVerdict.NEEDS_REVIEW in verdicts:
        return ESCALATE

    decision = next_recovery_step(
        retry_count=state.get("retry_count", 0),
        replan_count=state.get("replan_count", 0),
        failure_is_retryable=_last_failure_is_retryable(state),
        policy=policy,
    )
    if decision.outcome == BudgetOutcome.RETRY:
        return EXECUTE
    if decision.outcome == BudgetOutcome.REPLAN:
        return PLANNER
    return ESCALATE


def _last_failure_is_retryable(state: WorkflowState) -> bool:
    """Whether the most recent error was transient.

    Absent any recorded error, treat the failure as non-retryable: a validation
    failure with no explanation is not evidence of a transient fault, and
    guessing "transient" is the guess that retries a wrong action.
    """
    errors = state.get("errors") or []
    if not errors:
        return False
    return bool(errors[-1].get("retryable", False))
