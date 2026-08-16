"""Tool handlers over the enterprise service layer.

Handlers call the **same service functions** the HTTP routers call — tools are a
protocol adapter, never a second implementation of the business logic. Two
implementations would eventually disagree, and the one an agent uses would be
the one nobody exercised by hand.

Nine of the eleven tools in §8 are implemented here. ``create_refund`` and
``send_notification`` are deliberately absent rather than stubbed: neither has a
system behind it yet (no refund flow, no delivery mechanism), and a tool that
reports success without doing anything is exactly the failure a Validator exists
to catch (Rule 6). They arrive with the phases that build what they need.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from custops.apps.enterprise.billing import service as billing_service
from custops.apps.enterprise.contracts import service as contract_service
from custops.apps.enterprise.crm import service as crm_service
from custops.apps.enterprise.support import service as support_service
from custops.domain.policies.retrieval import RetrievalPolicy
from custops.knowledge.retrieval.search import retrieve_evidence
from custops.mcp.tools.results import ToolErrorCode, ToolExecutionError
from custops.mcp.tools.runtime import ToolContext
from custops.mcp.tools.schemas import (
    AccountInput,
    ContractOutput,
    CustomerOutput,
    EvidenceItemOutput,
    GetCustomerInput,
    GetEntitlementOutput,
    GetPricingInput,
    GetSupportHistoryInput,
    InvoiceListOutput,
    InvoiceOutput,
    PlanOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    SubscriptionOutput,
    SupportSummaryOutput,
    UpdateCrmInput,
    UpdateCrmOutput,
    UpdateEntitlementInput,
    UpdateEntitlementOutput,
    UpdateSubscriptionInput,
    UpdateSubscriptionOutput,
)
from custops.providers.base import EmbeddingProvider
from custops.provisioning.client import (
    ProvisioningClient,
    ProvisioningError,
    ProvisioningErrorCode,
)


async def get_customer(context: ToolContext, arguments: GetCustomerInput) -> CustomerOutput:
    customer = await crm_service.get_customer_by_ref(context.session, arguments.external_ref)
    if customer is None:
        raise ToolExecutionError(
            ToolErrorCode.NOT_FOUND,
            f"No customer with reference '{arguments.external_ref}'.",
            external_ref=arguments.external_ref,
        )
    return CustomerOutput(
        id=customer.id,
        external_ref=customer.external_ref,
        name=customer.name,
        status=customer.status,
        account_ids=[account.id for account in customer.accounts],
    )


async def get_subscription(context: ToolContext, arguments: AccountInput) -> SubscriptionOutput:
    subscription = await billing_service.get_active_subscription(
        context.session, arguments.account_id
    )
    if subscription is None:
        raise ToolExecutionError(
            ToolErrorCode.NOT_FOUND,
            f"Account {arguments.account_id} has no active subscription.",
        )
    return SubscriptionOutput(
        id=subscription.id,
        account_id=subscription.account_id,
        plan_code=subscription.plan.code,
        status=subscription.status,
        billing_cycle=subscription.billing_cycle,
        seats=subscription.seats,
        current_period_start=subscription.current_period_start,
        current_period_end=subscription.current_period_end,
    )


async def get_contract(context: ToolContext, arguments: AccountInput) -> ContractOutput:
    contract = await contract_service.get_active_contract(
        context.session, arguments.account_id, now=datetime.now(UTC)
    )
    if contract is None:
        raise ToolExecutionError(
            ToolErrorCode.NOT_FOUND,
            f"Account {arguments.account_id} has no contract in force.",
        )
    return ContractOutput(
        contract_number=contract.contract_number,
        status=contract.status,
        starts_at=contract.starts_at,
        ends_at=contract.ends_at,
        upgrade_restriction=contract.upgrade_restriction,
        notice_period_days=contract.notice_period_days,
    )


async def get_pricing(context: ToolContext, arguments: GetPricingInput) -> PlanOutput:
    plan = await billing_service.get_plan_by_code(context.session, arguments.plan_code)
    if plan is None:
        raise ToolExecutionError(
            ToolErrorCode.NOT_FOUND, f"No plan with code '{arguments.plan_code}'."
        )
    return PlanOutput(
        code=plan.code,
        name=plan.name,
        rank=plan.rank,
        monthly_price=plan.monthly_price,
        annual_price=plan.annual_price,
        currency=plan.currency,
        is_active=plan.is_active,
    )


async def get_invoice(context: ToolContext, arguments: AccountInput) -> InvoiceListOutput:
    now = datetime.now(UTC)
    invoices = await billing_service.list_invoices(context.session, arguments.account_id)
    past_due = await billing_service.count_past_due_invoices(
        context.session, arguments.account_id, now=now
    )
    return InvoiceListOutput(
        account_id=arguments.account_id,
        invoices=[
            InvoiceOutput(
                number=invoice.number,
                status=invoice.status,
                amount_due=invoice.amount_due,
                amount_paid=invoice.amount_paid,
                currency=invoice.currency,
                due_at=invoice.due_at,
            )
            for invoice in invoices
        ],
        past_due_count=past_due,
    )


async def get_support_history(
    context: ToolContext, arguments: GetSupportHistoryInput
) -> SupportSummaryOutput:
    summary = await support_service.summarise_support(context.session, arguments.account_id)
    return SupportSummaryOutput(
        account_id=arguments.account_id,
        total_tickets=summary.total_tickets,
        unresolved_tickets=summary.unresolved_tickets,
        open_urgent_tickets=summary.open_urgent_tickets,
        average_satisfaction=summary.average_satisfaction,
    )


SearchKnowledgeHandler = Callable[
    [ToolContext, SearchKnowledgeInput], Awaitable[SearchKnowledgeOutput]
]


def make_search_knowledge(
    provider: EmbeddingProvider, policy: RetrievalPolicy | None = None
) -> SearchKnowledgeHandler:
    """Bind an embedding provider — and its sufficiency policy — into the handler.

    A closure rather than a global: the provider is chosen by configuration and
    must be injectable in tests, and a module-level provider would embed a
    process-wide singleton into every call site.

    ``policy`` travels with the provider because a similarity threshold is a
    property of the *embedding model*, not of the business rule. ``None`` means
    the production default (``RetrievalPolicy()``, minimum similarity 0.35),
    which is correct for a real embedding model. A different provider — the
    deterministic lexical double, say — produces scores on a different scale and
    needs a threshold calibrated to it, or the gate reads every result as
    insufficient. What must never change is the *shape* of the gate: weak
    evidence escalates.
    """

    async def search_knowledge(
        context: ToolContext, arguments: SearchKnowledgeInput
    ) -> SearchKnowledgeOutput:
        evidence = await retrieve_evidence(
            context.session,
            provider,
            arguments.query,
            limit=arguments.limit,
            account_id=arguments.account_id,
            policy=policy,
        )
        return SearchKnowledgeOutput(
            query=evidence.query,
            sufficient=evidence.sufficient,
            confidence=evidence.confidence,
            reason=evidence.reason,
            items=[
                EvidenceItemOutput(
                    source=item.source,
                    source_ref=item.source_ref,
                    citation=item.citation,
                    content=item.content,
                    similarity=item.similarity,
                )
                for item in evidence.items
            ],
        )

    return search_knowledge


# --- Mutating handlers ----------------------------------------------------
# Reached only through execute_tool, which has already verified permission and
# consumed an approval scoped to this execution and this entity.


async def update_subscription(
    context: ToolContext, arguments: UpdateSubscriptionInput
) -> UpdateSubscriptionOutput:
    subscription = await billing_service.get_subscription(
        context.session, arguments.subscription_id
    )
    if subscription is None:
        raise ToolExecutionError(
            ToolErrorCode.NOT_FOUND, f"No subscription {arguments.subscription_id}."
        )

    target = await billing_service.get_plan_by_code(context.session, arguments.target_plan_code)
    if target is None:
        raise ToolExecutionError(
            ToolErrorCode.NOT_FOUND, f"No plan with code '{arguments.target_plan_code}'."
        )
    if not target.is_active:
        raise ToolExecutionError(
            ToolErrorCode.PRECONDITION_FAILED,
            f"Plan '{target.code}' is not available for new subscriptions.",
        )

    previous = subscription.plan.code
    await billing_service.apply_plan_change(context.session, subscription.id, target.id)

    return UpdateSubscriptionOutput(
        subscription_id=subscription.id,
        previous_plan_code=previous,
        new_plan_code=target.code,
    )


def make_update_entitlement(
    client: ProvisioningClient,
) -> Callable[[ToolContext, UpdateEntitlementInput], Awaitable[UpdateEntitlementOutput]]:
    """Bind a provisioning client into the entitlement-flip handler.

    The legacy portal has no API, so this is the browser step (§11, D8). It is
    still an ordinary MCP tool: permission-checked, approval-gated and audited
    like every other mutation. Having no API is not a reason to escape the
    boundary — if anything it is a reason to insist on it, since a browser
    driver is the least observable thing in the system.
    """

    async def update_entitlement(
        context: ToolContext, arguments: UpdateEntitlementInput
    ) -> UpdateEntitlementOutput:
        try:
            result = await client.set_tier(
                account_id=str(arguments.account_id),
                tier=arguments.tier,
                seats=arguments.seats,
            )
        except ProvisioningError as error:
            raise ToolExecutionError(
                _PROVISIONING_ERROR_CODES.get(error.code, ToolErrorCode.UPSTREAM_ERROR),
                error.message,
                portal_error=str(error.code),
            ) from error

        return UpdateEntitlementOutput(
            account_id=arguments.account_id,
            requested_tier=result.requested_tier,
            confirmed_tier=result.confirmed_tier,
            seats=result.seats,
            matches_request=result.matches_request,
            confirmation_text=result.confirmation_text,
        )

    return update_entitlement


def make_get_entitlement(
    client: ProvisioningClient,
) -> Callable[[ToolContext, AccountInput], Awaitable[GetEntitlementOutput]]:
    """Read the provisioned tier from the portal, for validation (§14)."""

    async def get_entitlement(
        context: ToolContext, arguments: AccountInput
    ) -> GetEntitlementOutput:
        try:
            tier = await client.read_tier(account_id=str(arguments.account_id))
        except ProvisioningError as error:
            raise ToolExecutionError(
                _PROVISIONING_ERROR_CODES.get(error.code, ToolErrorCode.UPSTREAM_ERROR),
                error.message,
                portal_error=str(error.code),
            ) from error

        return GetEntitlementOutput(account_id=arguments.account_id, tier=tier)

    return get_entitlement


# Portal failures mapped to the codes the graph routes on. A timeout is
# transient and worth retrying; a rejected tier or a missing account is not.
_PROVISIONING_ERROR_CODES = {
    ProvisioningErrorCode.TIMEOUT: ToolErrorCode.UPSTREAM_TIMEOUT,
    ProvisioningErrorCode.LOGIN_FAILED: ToolErrorCode.UPSTREAM_ERROR,
    ProvisioningErrorCode.BROWSER_UNAVAILABLE: ToolErrorCode.UPSTREAM_ERROR,
    ProvisioningErrorCode.ACCOUNT_NOT_FOUND: ToolErrorCode.NOT_FOUND,
    ProvisioningErrorCode.TIER_REJECTED: ToolErrorCode.PRECONDITION_FAILED,
    ProvisioningErrorCode.CONFIRMATION_MISSING: ToolErrorCode.PRECONDITION_FAILED,
    ProvisioningErrorCode.UNEXPECTED: ToolErrorCode.INTERNAL_ERROR,
}


async def update_crm(context: ToolContext, arguments: UpdateCrmInput) -> UpdateCrmOutput:
    changed_at = datetime.now(UTC)
    account = await crm_service.update_account_plan_reference(
        context.session, arguments.account_id, arguments.plan_code, changed_at
    )
    if account is None:
        raise ToolExecutionError(ToolErrorCode.NOT_FOUND, f"No account {arguments.account_id}.")
    return UpdateCrmOutput(
        account_id=account.id,
        current_plan_code=arguments.plan_code,
        changed_at=changed_at,
    )
