"""Structured-output schemas for the model-facing agents.

Every model call returns an instance of one of these. Nothing in this system
parses model prose: a classification that arrives as a validated object either
matched the schema or raised, whereas a classification extracted from a sentence
can be subtly wrong in ways nothing detects until it routes a workflow somewhere
unintended.

Note what these schemas do **not** contain: any field for the model's reasoning
process. ``rationale_summary`` is a short statement of the conclusion, and it is
the only free text stored (Rule 18, §16).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from custops.agents.state import WorkflowType


class RequestClassification(BaseModel):
    """The Supervisor's reading of an incoming request (§6).

    ``confidence`` is an input to the approval policy, never a licence to skip
    a gate. Low confidence can only ever add caution.
    """

    workflow_type: WorkflowType = Field(
        default=WorkflowType.UNKNOWN,
        description="The workflow this request maps to, or 'unknown' if unclear.",
    )
    customer_ref: str | None = Field(
        default=None, description="Customer handle mentioned in the request, e.g. 'ACME'."
    )
    target_plan_code: str | None = Field(
        default=None, description="Plan the customer should move to, e.g. 'enterprise'."
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale_summary: str = Field(
        default="", max_length=500, description="One-sentence conclusion. Not reasoning."
    )


class PlannedStep(BaseModel):
    name: str
    tool: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    parallelisable: bool = False
    likely_requires_approval: bool = False


class PlanDraft(BaseModel):
    """The Planner's structured output (§6)."""

    workflow_type: WorkflowType = WorkflowType.UNKNOWN
    steps: list[PlannedStep] = Field(default_factory=list)
    rationale_summary: str = Field(default="", max_length=500)


class NotificationDraft(BaseModel):
    """Customer-facing confirmation text.

    Drafting prose is a legitimate use of a model (§12). Whether the notification
    may be *sent* is not the model's call.
    """

    subject: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=4000)
