"""Transport schemas for the approval API (§13)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApprovalOut(BaseModel):
    """An approval request as a human sees it.

    Carries all six things §13 requires an approval request to contain: entity,
    proposed action, reason, supporting evidence, risk assessment, and expected
    outcome. A reviewer asked to authorise something should not have to go
    looking for what they are authorising.
    """

    id: uuid.UUID
    execution_id: uuid.UUID
    action: str
    entity_type: str
    entity_id: str
    status: str

    reason: str
    risk_assessment: str | None
    expected_outcome: str | None
    evidence: dict[str, Any]
    amount: Decimal | None

    requested_at: datetime
    decided_at: datetime | None
    decided_by_user_id: uuid.UUID | None
    decision_note: str | None
    consumed_at: datetime | None


class ApprovalDecisionRequest(BaseModel):
    """A human's decision.

    **Carries no identity.** Who decided comes from the bearer token's
    principal, and `model_config` forbids extra fields so a client that still
    sends ``actor_user_id`` is rejected outright rather than having it silently
    ignored — a caller who believes they are choosing the actor should be told
    they are not.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    note: str | None = Field(default=None, max_length=1000)


class ApprovalDecisionOut(BaseModel):
    """The recorded decision, and what the workflow did next."""

    approval: ApprovalOut
    workflow_status: str
    workflow_resumed: bool
    # Present when resuming produced a further pause — a workflow can need more
    # than one human decision.
    still_awaiting_approval: bool = False
