"""Enterprise read API.

**Read-only, deliberately.** Every endpoint here is a `GET`. The mutating
service functions exist (`apply_plan_change`, `update_account_plan_reference`)
but are not routed, because the approval architecture depends on mutations
travelling through the MCP tool layer, which independently verifies an approval
record before acting (decision D9). A `PATCH /subscriptions/{id}` here would be a
documented, unguarded bypass of that — the exact hole §13's three-layer
enforcement exists to close.

These endpoints exist so a human (or a test) can inspect the systems of record
directly, which is also how one verifies the Validator's cross-system checks are
looking at the same data a person would see.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from custops.apps.api.dependencies import get_database
from custops.apps.enterprise import assessment as assessment_module
from custops.apps.enterprise.billing import service as billing_service
from custops.apps.enterprise.contracts import service as contract_service
from custops.apps.enterprise.crm import service as crm_service
from custops.apps.enterprise.schemas import (
    ContractOut,
    CustomerOut,
    PlanOut,
    SubscriptionOut,
    SupportSummaryOut,
    UpgradeAssessmentOut,
)
from custops.apps.enterprise.support import service as support_service
from custops.db.engine import Database

router = APIRouter(prefix="/enterprise", tags=["enterprise"])


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Yield a session for the duration of the request.

    Must ``yield``, not ``return``: returning from inside the ``async with``
    would close the session before the handler ever used it, and every query
    would fail on a closed connection. FastAPI resumes the generator to close it
    once the response is done.
    """
    async with database.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/plans", response_model=list[PlanOut], summary="List purchasable plans")
async def list_plans(session: SessionDep, active_only: bool = True) -> list[PlanOut]:
    plans = await billing_service.list_plans(session, active_only=active_only)
    return [PlanOut.model_validate(plan) for plan in plans]


@router.get(
    "/customers/{external_ref}",
    response_model=CustomerOut,
    summary="Look up a customer by its external reference",
)
async def get_customer(external_ref: str, session: SessionDep) -> CustomerOut:
    customer = await crm_service.get_customer_by_ref(session, external_ref)
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No customer with reference '{external_ref}'.",
        )
    return CustomerOut.model_validate(customer)


@router.get(
    "/accounts/{account_id}/subscription",
    response_model=SubscriptionOut,
    summary="The account's active subscription (billing's view of the plan)",
)
async def get_active_subscription(account_id: uuid.UUID, session: SessionDep) -> SubscriptionOut:
    subscription = await billing_service.get_active_subscription(session, account_id)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} has no active subscription.",
        )
    return SubscriptionOut.model_validate(subscription)


@router.get(
    "/accounts/{account_id}/contract",
    response_model=ContractOut,
    summary="The contract currently in force",
)
async def get_contract(account_id: uuid.UUID, session: SessionDep) -> ContractOut:
    contract = await contract_service.get_active_contract(
        session, account_id, now=datetime.now(UTC)
    )
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} has no contract in force.",
        )
    return ContractOut.model_validate(contract)


@router.get(
    "/accounts/{account_id}/support-summary",
    response_model=SupportSummaryOut,
    summary="Aggregate support posture for an account",
)
async def get_support_summary(account_id: uuid.UUID, session: SessionDep) -> SupportSummaryOut:
    summary = await support_service.summarise_support(session, account_id)
    return SupportSummaryOut(
        total_tickets=summary.total_tickets,
        unresolved_tickets=summary.unresolved_tickets,
        open_urgent_tickets=summary.open_urgent_tickets,
        average_satisfaction=summary.average_satisfaction,
    )


@router.get(
    "/accounts/{account_id}/upgrade-assessment",
    response_model=UpgradeAssessmentOut,
    summary="Deterministic verdict on a proposed upgrade, with evidence",
)
async def get_upgrade_assessment(
    account_id: uuid.UUID,
    session: SessionDep,
    target_plan_code: Annotated[str, Query(min_length=1, max_length=32)],
) -> UpgradeAssessmentOut:
    """Assess an upgrade without performing it.

    Pure computation over stored state: it changes nothing, so it is safe to
    expose read-only. This is also the endpoint that makes the deterministic
    layer inspectable — a reviewer can see the verdict and its evidence without
    running an agent at all.
    """
    try:
        result = await assessment_module.assess_upgrade(
            session,
            account_id=account_id,
            target_plan_code=target_plan_code,
            now=datetime.now(UTC),
        )
    except assessment_module.AssessmentError as error:
        # "The question could not be asked" is a 404, distinct from "the answer
        # is no", which is a 200 with blockers.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": str(error.code), "message": error.message},
        ) from error

    return UpgradeAssessmentOut.from_assessment(result)
