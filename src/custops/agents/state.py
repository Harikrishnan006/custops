"""The workflow state passed between graph nodes (BUILD_SPEC §7).

Two design rules govern what may live here.

**Accumulating fields use reducers.** Nodes return partial updates; LangGraph
merges them. A field annotated with a reducer (``Annotated[list[X], add]``) has
each node's contribution appended, which is what makes parallel fan-out safe —
two research branches returning evidence concurrently both land, rather than the
second overwriting the first. Scalar fields are last-write-wins, which is
correct for things like ``status`` that describe the run as a whole.

**Never store chain-of-thought** (§7, Rule 18). What is stored is structured:
decisions with codes, evidence with citations, tool inputs and outputs where
safe, validation results, and short rationale *summaries*. The distinction is
not cosmetic — this state is checkpointed to PostgreSQL and reconstructed into
an audit trace, so anything written here is retained and disclosable.
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, TypedDict


class WorkflowStatus(StrEnum):
    """Where the run is. Set by Python, never by a model."""

    RECEIVED = "received"
    PLANNING = "planning"
    RESEARCHING = "researching"
    DECIDING = "deciding"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"


class WorkflowType(StrEnum):
    """Workflows this platform can run.

    Only Subscription Upgrade ships (decision D3). ``UNKNOWN`` is what the
    Supervisor produces when it cannot classify a request — an explicit value
    that routes to escalation, rather than a guess that routes into a workflow
    the request was never about.
    """

    SUBSCRIPTION_UPGRADE = "subscription_upgrade"
    UNKNOWN = "unknown"


class ValidationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class ApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    GRANTED = "granted"
    REJECTED = "rejected"


class PlanStep(TypedDict):
    """One step of a structured plan (§6)."""

    name: str
    tool: str | None
    depends_on: list[str]
    parallelisable: bool
    likely_requires_approval: bool


class Plan(TypedDict):
    workflow_type: str
    steps: list[PlanStep]
    rationale_summary: str


class Decision(TypedDict):
    """A decision, recorded structurally.

    ``rationale_summary`` is a short conclusion — never the reasoning that
    produced it.
    """

    name: str
    outcome: str
    confidence: float
    rationale_summary: str
    evidence_refs: list[str]
    decided_at: str


class ExecutionResult(TypedDict):
    step: str
    tool: str
    ok: bool
    error_code: str | None
    detail: dict[str, Any]


class ValidationResult(TypedDict):
    """One expected-vs-actual comparison, read from a system of record."""

    check: str
    system: str
    verdict: str
    expected: str
    actual: str


class WorkflowError(TypedDict):
    stage: str
    code: str
    message: str
    retryable: bool


class WorkflowState(TypedDict, total=False):
    """State shared by every node.

    ``total=False`` because nodes return partial updates; LangGraph merges each
    into the whole. Fields annotated with ``operator.add`` accumulate.
    """

    # --- Identity, set once at entry -------------------------------------
    execution_id: uuid.UUID
    request_id: str
    raw_request: str
    started_at: datetime

    # --- Classification and planning -------------------------------------
    workflow_type: str
    plan: Plan | None

    # --- Subject of the workflow -----------------------------------------
    customer_ref: str | None
    account_id: uuid.UUID | None
    target_plan_code: str | None

    # --- Accumulating: every node's contribution is kept ------------------
    evidence: Annotated[list[dict[str, Any]], operator.add]
    decisions: Annotated[list[Decision], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    execution_results: Annotated[list[ExecutionResult], operator.add]
    validation_results: Annotated[list[ValidationResult], operator.add]
    errors: Annotated[list[WorkflowError], operator.add]

    # --- Approval (§13) ---------------------------------------------------
    approval_status: str
    approval_id: uuid.UUID | None

    # --- Budgets, enforced in Python (§7) ---------------------------------
    retry_count: int
    replan_count: int

    # --- Outcome ----------------------------------------------------------
    status: str
    escalation_reason: str | None
    metadata: dict[str, Any]


def initial_state(
    *,
    execution_id: uuid.UUID,
    request_id: str,
    raw_request: str,
    started_at: datetime,
) -> WorkflowState:
    """A fresh run.

    Every accumulating field starts as an empty list rather than absent, so a
    node reading ``state["evidence"]`` before any evidence exists gets an empty
    list instead of a KeyError.
    """
    return WorkflowState(
        execution_id=execution_id,
        request_id=request_id,
        raw_request=raw_request,
        started_at=started_at,
        workflow_type=WorkflowType.UNKNOWN,
        plan=None,
        customer_ref=None,
        account_id=None,
        target_plan_code=None,
        evidence=[],
        decisions=[],
        tool_calls=[],
        execution_results=[],
        validation_results=[],
        errors=[],
        approval_status=ApprovalState.NOT_REQUIRED,
        approval_id=None,
        retry_count=0,
        replan_count=0,
        status=WorkflowStatus.RECEIVED,
        escalation_reason=None,
        metadata={},
    )
