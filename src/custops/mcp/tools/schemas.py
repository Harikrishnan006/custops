"""Input and output schemas for every tool (BUILD_SPEC §8).

Every tool has a Pydantic input schema and a Pydantic output schema. Inputs are
validated before a handler runs, so a malformed argument is a structured
``invalid_input`` failure rather than a TypeError halfway through a mutation.
Outputs are declared so an agent's context receives a known shape rather than
whatever an ORM row happened to serialise to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

# --- Inputs ---------------------------------------------------------------


class GetCustomerInput(BaseModel):
    external_ref: str = Field(min_length=1, max_length=64, description="e.g. 'ACME'")


class AccountInput(BaseModel):
    account_id: uuid.UUID


class GetPricingInput(BaseModel):
    plan_code: str = Field(min_length=1, max_length=32)


class SearchKnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    # Scoping is a correctness boundary: without it a search can surface another
    # customer's contract. Optional because policy-only searches are legitimate.
    account_id: uuid.UUID | None = None


class GetSupportHistoryInput(BaseModel):
    account_id: uuid.UUID
    limit: int = Field(default=20, ge=1, le=100)


class UpdateSubscriptionInput(BaseModel):
    subscription_id: uuid.UUID
    target_plan_code: str = Field(min_length=1, max_length=32)


class UpdateCrmInput(BaseModel):
    account_id: uuid.UUID
    plan_code: str = Field(min_length=1, max_length=32)


# --- Outputs --------------------------------------------------------------


class CustomerOutput(BaseModel):
    id: uuid.UUID
    external_ref: str
    name: str
    status: str
    account_ids: list[uuid.UUID]


class PlanOutput(BaseModel):
    code: str
    name: str
    rank: int
    monthly_price: Decimal
    annual_price: Decimal
    currency: str
    is_active: bool


class SubscriptionOutput(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    plan_code: str
    status: str
    billing_cycle: str
    seats: int
    current_period_start: datetime
    current_period_end: datetime


class ContractOutput(BaseModel):
    contract_number: str
    status: str
    starts_at: datetime
    ends_at: datetime
    upgrade_restriction: str
    notice_period_days: int


class InvoiceOutput(BaseModel):
    number: str
    status: str
    amount_due: Decimal
    amount_paid: Decimal
    currency: str
    due_at: datetime | None


class InvoiceListOutput(BaseModel):
    account_id: uuid.UUID
    invoices: list[InvoiceOutput]
    past_due_count: int


class SupportSummaryOutput(BaseModel):
    account_id: uuid.UUID
    total_tickets: int
    unresolved_tickets: int
    open_urgent_tickets: int
    average_satisfaction: float | None


class EvidenceItemOutput(BaseModel):
    source: str
    source_ref: str
    citation: str
    content: str
    similarity: float | None


class SearchKnowledgeOutput(BaseModel):
    """Structured evidence with source references, never prose (§6)."""

    query: str
    sufficient: bool
    confidence: float
    reason: str
    items: list[EvidenceItemOutput]


class UpdateSubscriptionOutput(BaseModel):
    subscription_id: uuid.UUID
    previous_plan_code: str
    new_plan_code: str
    approval_id: uuid.UUID | None = None


class UpdateCrmOutput(BaseModel):
    account_id: uuid.UUID
    current_plan_code: str
    changed_at: datetime
