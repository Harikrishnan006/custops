"""Transport schemas for the approval API (§13)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


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

    ``actor_user_id`` identifies who decided. **It is asserted, not proven** —
    authentication arrives in Phase 13. Until then this endpoint enforces
    *authorisation* (does this user hold an approving role?) but cannot verify
    identity, and the audit trail is only as trustworthy as the caller.
    """

    approved: bool
    actor_user_id: uuid.UUID
    note: str | None = Field(default=None, max_length=1000)


class ApprovalDecisionOut(BaseModel):
    """The recorded decision, and what the workflow did next."""

    approval: ApprovalOut
    workflow_status: str
    workflow_resumed: bool
    # Present when resuming produced a further pause — a workflow can need more
    # than one human decision.
    still_awaiting_approval: bool = False
