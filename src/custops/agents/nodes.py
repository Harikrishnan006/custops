"""The ten node implementations behind ``NodeSet``.

Wiring, and the boundaries that matter:

* **Every node opens its own session and commits.** A session cannot span an
  interrupt — the approval gate may pause for days — so holding one across nodes
  would either fail or pin a connection for the duration of a human's lunch.
* **Reads go through MCP tools**, not straight to the services, so agent access
  is permission-checked and audited exactly as §8 requires. The tool layer is
  the boundary whether the caller is a model or a graph node.
* **Nothing here decides anything consequential.** Eligibility, pricing and the
  approval requirement come from ``domain/rules`` and ``domain/policies``; the
  model classifies, plans, and drafts prose.
* **No chain-of-thought is stored.** Decisions carry a code, an outcome, a
  confidence and a one-line ``rationale_summary`` — never the reasoning that
  produced them (Rule 18).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from custops.a2a.client.billing import (
    BillingSpecialistClient,
    ConsultResult,
    ConsultStatus,
    corroborates,
)
from custops.agents.schemas import NotificationDraft, PlanDraft, RequestClassification
from custops.agents.state import (
    ApprovalState,
    Decision,
    ValidationVerdict,
    WorkflowState,
    WorkflowStatus,
    WorkflowType,
)
from custops.agents.validation import (
    ExpectedState,
    ObservedState,
    divergent_systems,
    overall_verdict,
    validate_upgrade,
)
from custops.apps.enterprise import assessment as assessment_module
from custops.apps.enterprise.crm import service as crm_service
from custops.apps.orchestrator.graph import NodeSet
from custops.domain.models.approval import Approval, ApprovalStatus
from custops.domain.models.billing import Subscription
from custops.domain.policies.thresholds import ApprovalPolicy
from custops.mcp.permissions.matrix import Role, ToolName
from custops.mcp.tools import enterprise as handlers
from custops.mcp.tools.results import ToolErrorCode
from custops.mcp.tools.runtime import ToolContext, execute_tool
from custops.mcp.tools.schemas import (
    AccountInput,
    GetSupportHistoryInput,
    SearchKnowledgeInput,
    UpdateCrmInput,
    UpdateEntitlementInput,
    UpdateSubscriptionInput,
)
from custops.observability.audit import record_event
from custops.observability.events import ActorType, EventType
from custops.observability.logging import get_logger
from custops.providers.base import EmbeddingProvider
from custops.providers.chat import ChatProvider
from custops.provisioning.client import ProvisioningClient

logger = get_logger(__name__)

SUPERVISOR_SYSTEM = (
    "Classify a B2B SaaS customer-operations request. Return the workflow it maps "
    "to, the customer handle, and the target plan. If the request is unclear, "
    "return workflow_type 'unknown' rather than guessing."
)
PLANNER_SYSTEM = (
    "Produce an ordered plan of steps for the classified workflow. Name the tool "
    "each step uses where one applies. Do not invent steps for systems that were "
    "not mentioned."
)
NOTIFIER_SYSTEM = (
    "Draft a short, factual confirmation message for a business customer. State "
    "what changed and what it costs. No marketing language."
)


@dataclass(frozen=True, slots=True)
class NodeDependencies:
    """Everything the nodes need from outside.

    Injected rather than imported so a test can drive any path with a
    deterministic model and a real or absent database.
    """

    session_factory: async_sessionmaker[AsyncSession]
    chat: ChatProvider
    embedder: EmbeddingProvider
    # Drives the legacy portal (§11). Optional so a run can be assembled without
    # a browser; the execute step then fails honestly rather than skipping
    # provisioning and letting validation call the result a success.
    provisioning: ProvisioningClient | None = None
    # The out-of-process Billing Specialist (§9, D6). Optional in the strong
    # sense: when it is absent or unreachable the workflow reaches the same
    # decision from the same local rules, and records that it went unconsulted.
    billing_specialist: BillingSpecialistClient | None = None
    approval_policy: ApprovalPolicy | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def _decision(
    name: str,
    outcome: str,
    confidence: float,
    summary: str,
    evidence_refs: list[str],
    now: datetime,
) -> Decision:
    return Decision(
        name=name,
        outcome=outcome,
        confidence=confidence,
        rationale_summary=summary,
        evidence_refs=evidence_refs,
        decided_at=now.isoformat(),
    )


async def _emit(
    deps: NodeDependencies,
    state: WorkflowState,
    event_type: EventType,
    *,
    actor_type: ActorType = ActorType.AGENT,
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record a workflow event from a node that holds no session.

    Opens and commits its own session, matching this module's rule that a
    session never spans nodes. An audit write that failed to commit would leave
    a trace claiming less happened than did, so it commits independently of
    whatever the node goes on to do.
    """
    async with deps.session_factory() as session:
        await record_event(
            session,
            event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            execution_id=state.get("execution_id"),
            request_id=state.get("request_id"),
        )
        await session.commit()


def _tool_context(session: AsyncSession, state: WorkflowState, role: str) -> ToolContext:
    return ToolContext(
        session=session,
        role=role,
        execution_id=state.get("execution_id"),
        request_id=state.get("request_id"),
    )


def build_nodes(deps: NodeDependencies) -> NodeSet:
    """Construct the node set bound to these dependencies."""

    # ---------------------------------------------------------------- supervisor
    async def supervisor(state: WorkflowState) -> dict[str, Any]:
        """Classify the request. Never acts on it (§6)."""
        classification = await deps.chat.structured(
            system=SUPERVISOR_SYSTEM,
            user=state["raw_request"],
            schema=RequestClassification,
        )
        now = deps.clock()

        await _emit(
            deps,
            state,
            EventType.WORKFLOW_CLASSIFIED,
            actor_id=Role.SUPERVISOR,
            entity_type="workflow_execution",
            entity_id=str(state.get("execution_id")),
            payload={
                "workflow_type": str(classification.workflow_type),
                "customer_ref": classification.customer_ref,
                "target_plan_code": classification.target_plan_code,
                "confidence": classification.confidence,
            },
        )

        return {
            "workflow_type": classification.workflow_type,
            "customer_ref": classification.customer_ref,
            "target_plan_code": classification.target_plan_code,
            "status": WorkflowStatus.PLANNING,
            "decisions": [
                _decision(
                    "request_classification",
                    classification.workflow_type,
                    classification.confidence,
                    classification.rationale_summary or "Request classified.",
                    [],
                    now,
                )
            ],
        }

    # ------------------------------------------------------------------- planner
    async def planner(state: WorkflowState) -> dict[str, Any]:
        draft = await deps.chat.structured(
            system=PLANNER_SYSTEM,
            user=f"Request: {state['raw_request']}\nWorkflow: {state.get('workflow_type')}",
            schema=PlanDraft,
        )

        await _emit(
            deps,
            state,
            EventType.PLAN_CREATED,
            actor_id=Role.PLANNER,
            entity_type="workflow_execution",
            entity_id=str(state.get("execution_id")),
            # Step names and count, not the planner's prose: a plan is a
            # conclusion, and the drafting that produced it is not stored.
            payload={
                "step_count": len(draft.steps),
                "steps": [step.model_dump().get("name") for step in draft.steps],
            },
        )

        return {
            "plan": {
                "workflow_type": str(draft.workflow_type),
                "steps": [step.model_dump() for step in draft.steps],
                "rationale_summary": draft.rationale_summary,
            },
            "status": WorkflowStatus.RESEARCHING,
        }

    # ------------------------------------------------------------------ research
    async def research(state: WorkflowState) -> dict[str, Any]:
        """Gather structured evidence through the tool layer (§6).

        Returns evidence with source references and a sufficiency verdict
        computed by the deterministic retrieval policy — the model does not
        judge whether its own evidence was enough.
        """
        customer_ref = state.get("customer_ref")
        if not customer_ref:
            return _fail_research("No customer reference was identified in the request.")

        async with deps.session_factory() as session:
            customer = await crm_service.get_customer_by_ref(session, customer_ref)
            if customer is None or not customer.accounts:
                await session.commit()
                return _fail_research(f"No account found for customer '{customer_ref}'.")

            account_id = customer.accounts[0].id
            context = _tool_context(session, state, Role.RESEARCH)

            await record_event(
                session,
                EventType.RETRIEVAL_STARTED,
                actor_type=ActorType.AGENT,
                actor_id=Role.RESEARCH,
                entity_type="account",
                entity_id=str(account_id),
                payload={"customer_ref": customer_ref},
                execution_id=state.get("execution_id"),
                request_id=state.get("request_id"),
            )
            evidence: list[dict[str, Any]] = [
                {
                    "source": "account",
                    "source_ref": f"account:{account_id}",
                    "content": (
                        f"{customer.name} ({customer.external_ref}), status {customer.status}"
                    ),
                }
            ]
            errors: list[dict[str, Any]] = []

            subscription = await execute_tool(
                context,
                ToolName.GET_SUBSCRIPTION,
                AccountInput(account_id=account_id),
                handlers.get_subscription,
            )
            if subscription.ok and subscription.data is not None:
                evidence.append(
                    {
                        "source": "subscription",
                        "source_ref": f"subscription:{subscription.data.id}",
                        "content": f"plan {subscription.data.plan_code}, "
                        f"{subscription.data.seats} seats, {subscription.data.status}",
                    }
                )
            elif subscription.error is not None:
                errors.append(_error("research", subscription.error))

            invoices = await execute_tool(
                context,
                ToolName.GET_INVOICE,
                AccountInput(account_id=account_id),
                handlers.get_invoice,
            )
            if invoices.ok and invoices.data is not None:
                evidence.append(
                    {
                        "source": "invoice",
                        "source_ref": f"account:{account_id}#invoices",
                        "content": f"{invoices.data.past_due_count} past-due invoice(s)",
                    }
                )

            support = await execute_tool(
                context,
                ToolName.GET_SUPPORT_HISTORY,
                GetSupportHistoryInput(account_id=account_id),
                handlers.get_support_history,
            )
            if support.ok and support.data is not None:
                evidence.append(
                    {
                        "source": "support",
                        "source_ref": f"account:{account_id}#support",
                        "content": f"{support.data.open_urgent_tickets} open urgent ticket(s)",
                    }
                )

            # Retrieval over policies and this account's contracts. Sufficiency
            # comes back from the deterministic rule, not from the model.
            knowledge = await execute_tool(
                context,
                ToolName.SEARCH_KNOWLEDGE,
                SearchKnowledgeInput(
                    query=(
                        f"eligibility and contract terms for upgrading to "
                        f"{state.get('target_plan_code') or 'a higher plan'}"
                    ),
                    account_id=account_id,
                ),
                handlers.make_search_knowledge(deps.embedder),
            )

            sufficient = False
            confidence = 0.0
            reason = "Knowledge retrieval did not run."
            if knowledge.ok and knowledge.data is not None:
                sufficient = knowledge.data.sufficient
                confidence = knowledge.data.confidence
                reason = knowledge.data.reason
                evidence.extend(
                    {
                        "source": item.source,
                        "source_ref": item.citation,
                        "content": item.content,
                        "similarity": item.similarity,
                    }
                    for item in knowledge.data.items
                )
            elif knowledge.error is not None:
                errors.append(_error("research", knowledge.error))

            # Counts and the sufficiency verdict — never the retrieved text.
            # Evidence content already lives in the workflow state; duplicating
            # it here would bloat every trace for no added accountability.
            await record_event(
                session,
                EventType.RETRIEVAL_COMPLETED,
                actor_type=ActorType.AGENT,
                actor_id=Role.RESEARCH,
                entity_type="account",
                entity_id=str(account_id),
                payload={
                    "evidence_count": len(evidence),
                    "sources": sorted({str(item["source"]) for item in evidence}),
                    "sufficient": sufficient,
                    "confidence": confidence,
                    "reason": reason,
                    "error_count": len(errors),
                },
                execution_id=state.get("execution_id"),
                request_id=state.get("request_id"),
            )

            await session.commit()

        return {
            "account_id": account_id,
            "evidence": evidence,
            "errors": errors,
            "status": WorkflowStatus.DECIDING,
            "metadata": {
                **(state.get("metadata") or {}),
                "evidence_sufficient": sufficient,
                "retrieval_confidence": confidence,
                "retrieval_reason": reason,
            },
        }

    def _fail_research(reason: str) -> dict[str, Any]:
        return {
            "evidence": [],
            "errors": [
                {
                    "stage": "research",
                    "code": ToolErrorCode.NOT_FOUND,
                    "message": reason,
                    "retryable": False,
                }
            ],
            "metadata": {"evidence_sufficient": False, "retrieval_reason": reason},
        }

    # -------------------------------------------------------------------- decide
    async def decide(state: WorkflowState) -> dict[str, Any]:
        """Run the deterministic assessment (§12).

        The verdict, the price and whether a human is needed all come from
        ``domain/rules`` and ``domain/policies``. No model is consulted.
        """
        account_id = state.get("account_id")
        target = state.get("target_plan_code")
        now = deps.clock()

        if account_id is None or not target:
            return _escalate_update("Assessment needs an account and a target plan.")

        async with deps.session_factory() as session:
            try:
                assessment = await assessment_module.assess_upgrade(
                    session,
                    account_id=account_id,
                    target_plan_code=target,
                    now=now,
                    policy=deps.approval_policy,
                )
            except assessment_module.AssessmentError as error:
                await session.rollback()
                return {
                    "errors": [
                        {
                            "stage": "decide",
                            "code": str(error.code),
                            "message": error.message,
                            "retryable": False,
                        }
                    ],
                    "status": WorkflowStatus.ESCALATED,
                    "escalation_reason": error.message,
                    "approval_status": ApprovalState.NOT_REQUIRED,
                }
            await session.commit()

        blockers = [str(finding.code) for finding in assessment.eligibility.blockers]
        if blockers:
            await _emit(
                deps,
                state,
                EventType.DECISION_MADE,
                actor_id=Role.EXECUTION,
                entity_type="account",
                entity_id=str(account_id),
                payload={
                    "decision": "upgrade_eligibility",
                    "outcome": "blocked",
                    "blockers": blockers,
                },
            )
            return {
                "decisions": [
                    _decision(
                        "upgrade_eligibility",
                        "blocked",
                        1.0,
                        f"Blocked by: {', '.join(blockers)}.",
                        _evidence_refs(assessment),
                        now,
                    )
                ],
                "status": WorkflowStatus.ESCALATED,
                "escalation_reason": f"Upgrade blocked: {', '.join(blockers)}.",
            }

        # Consult the specialist only once the local rules have cleared the
        # upgrade. A blocked upgrade escalates regardless, so a network round
        # trip to corroborate a 'no' buys nothing.
        consult = await _consult_specialist(account_id, target, state)
        agrees, divergence = corroborates(consult, local_amount=assessment.proration.amount_due)

        # The specialist may only ever *raise* the approval bar. It reads a
        # subset of the data (no customer standing, no negotiated discounts), so
        # its 'no approval needed' is uninformed rather than reassuring — and a
        # remote agent that could clear a human gate would put the approval
        # decision outside the audited local path entirely (D9).
        needs_approval = assessment.approval.required
        extra_triggers: list[str] = []
        if consult.recommendation is not None and consult.recommendation.approval_indicated:
            needs_approval = True
            extra_triggers.append("specialist_indicated_approval")
        if divergence is not None:
            # Two systems reading the same account and pricing it differently is
            # precisely a human-review case: one of them is reading stale or
            # wrong state, and neither side can tell which from here.
            needs_approval = True
            extra_triggers.append("specialist_amount_divergence")

        decisions = [
            _decision(
                "upgrade_eligibility",
                "eligible",
                1.0,
                "All deterministic eligibility checks passed.",
                _evidence_refs(assessment),
                now,
            ),
            _decision(
                "pricing",
                str(assessment.proration.amount_due),
                1.0,
                f"Proration {assessment.proration.amount_due} "
                f"{assessment.proration.currency} for "
                f"{assessment.proration.days_remaining} of "
                f"{assessment.proration.days_in_period} days.",
                [],
                now,
            ),
        ]
        if deps.billing_specialist is not None:
            # Record the consultation whenever one was attempted — including
            # when it failed. "We asked and could not reach it" belongs in the
            # trace; silence would read as "we never needed a second opinion".
            decisions.append(
                _decision(
                    "billing_specialist_consultation",
                    str(consult.status),
                    consult.recommendation.confidence if consult.recommendation else 0.0,
                    divergence
                    or (consult.detail if consult.detail else "Specialist agreed on the amount."),
                    consult.recommendation.evidence_refs if consult.recommendation else [],
                    now,
                )
            )

        await _emit(
            deps,
            state,
            EventType.DECISION_MADE,
            actor_id=Role.EXECUTION,
            entity_type="account",
            entity_id=str(account_id),
            # Conclusions only: what was decided, what it costs, whether a human
            # is needed and why. Never how the assessment reasoned (Rule 18).
            payload={
                "decision": "upgrade_eligibility",
                "outcome": "eligible",
                "amount": str(assessment.proration.amount_due),
                "currency": assessment.proration.currency,
                "approval_required": needs_approval,
                "approval_triggers": [
                    *(str(t) for t in assessment.approval.triggers),
                    *extra_triggers,
                ],
                "specialist": consult.as_trace(),
            },
        )

        return {
            "decisions": decisions,
            "approval_status": (
                ApprovalState.REQUIRED if needs_approval else ApprovalState.NOT_REQUIRED
            ),
            "status": (
                WorkflowStatus.AWAITING_APPROVAL if needs_approval else WorkflowStatus.EXECUTING
            ),
            "metadata": {
                **(state.get("metadata") or {}),
                "proration_amount": str(assessment.proration.amount_due),
                "current_plan_code": assessment.current_plan_code,
                "approval_triggers": [
                    *(str(t) for t in assessment.approval.triggers),
                    *extra_triggers,
                ],
                "approval_reasons": list(assessment.approval.reasons),
                "billing_specialist": {**consult.as_trace(), "agrees": agrees},
            },
        }

    async def _consult_specialist(
        account_id: uuid.UUID, target: str, state: WorkflowState
    ) -> ConsultResult:
        """Ask the Billing Specialist, tolerating its absence.

        Not configured is reported the same way as not reachable: in both cases
        the workflow has no second opinion, and the trace should say so rather
        than distinguish a deployment choice from an outage.
        """
        if deps.billing_specialist is None:
            return ConsultResult(
                status=ConsultStatus.UNAVAILABLE,
                detail="No billing specialist is configured.",
            )
        # The pair brackets the process boundary. An agent-to-agent hop is
        # exactly where a correlation id goes missing, so both events are
        # written on *this* side under this execution — the specialist records
        # its own tool calls independently under the same id.
        await _emit(
            deps,
            state,
            EventType.A2A_REQUEST_SENT,
            actor_id="orchestrator",
            entity_type="agent",
            entity_id="billing-specialist",
            payload={
                "capability": "billing.pricing_decision",
                "account_id": str(account_id),
                "target_plan_code": target,
            },
        )

        result = await deps.billing_specialist.request_pricing_decision(
            account_id=account_id,
            target_plan_code=target,
            execution_id=state.get("execution_id"),
        )

        await _emit(
            deps,
            state,
            EventType.A2A_RESPONSE_RECEIVED,
            actor_id="orchestrator",
            entity_type="agent",
            entity_id="billing-specialist",
            payload=result.as_trace(),
        )
        return result

    def _escalate_update(reason: str) -> dict[str, Any]:
        return {
            "status": WorkflowStatus.ESCALATED,
            "escalation_reason": reason,
            "errors": [
                {
                    "stage": "decide",
                    "code": ToolErrorCode.INVALID_INPUT,
                    "message": reason,
                    "retryable": False,
                }
            ],
        }

    # ------------------------------------------------------------ approval_gate
    async def approval_gate(state: WorkflowState) -> dict[str, Any]:
        """Record the request, then pause for a human (§13).

        Writes a PENDING approval row *before* interrupting, so the decision a
        human eventually makes attaches to a durable record rather than to
        in-memory graph state. ``interrupt()`` suspends the run; the checkpointer
        is what lets it resume in a different process, hours later.

        The resumed value is the human's decision. Anything that is not an
        explicit approval is treated as a refusal.
        """
        now = deps.clock()
        execution_id = state["execution_id"]
        account_id = state.get("account_id")
        metadata = state.get("metadata") or {}

        approval_id = uuid.uuid4()
        async with deps.session_factory() as session:
            session.add(
                Approval(
                    id=approval_id,
                    execution_id=execution_id,
                    action="subscription_upgrade",
                    entity_type="account",
                    entity_id=str(account_id),
                    status=ApprovalStatus.PENDING,
                    reason=f"Upgrade to {state.get('target_plan_code')} requires approval.",
                    risk_assessment="; ".join(metadata.get("approval_reasons", [])) or None,
                    expected_outcome=(
                        f"Subscription and CRM move to {state.get('target_plan_code')}."
                    ),
                    evidence={
                        "citations": [e.get("source_ref") for e in state.get("evidence", [])]
                    },
                    amount=(
                        Decimal(metadata["proration_amount"])
                        if metadata.get("proration_amount")
                        else None
                    ),
                    requested_at=now,
                )
            )
            # Same transaction as the PENDING row: an approval that exists with
            # no audit trail, or an audited request with no row to decide on,
            # would each be worse than neither.
            await record_event(
                session,
                EventType.APPROVAL_REQUESTED,
                actor_type=ActorType.AGENT,
                actor_id=Role.EXECUTION,
                entity_type="account",
                entity_id=str(account_id),
                payload={
                    "approval_id": str(approval_id),
                    "action": "subscription_upgrade",
                    "target_plan_code": state.get("target_plan_code"),
                    "amount": metadata.get("proration_amount"),
                    "triggers": metadata.get("approval_triggers", []),
                },
                execution_id=execution_id,
                request_id=state.get("request_id"),
            )
            await session.commit()

        # Suspends here. The value below is what a human sees.
        decision = interrupt(
            {
                "approval_id": str(approval_id),
                "execution_id": str(execution_id),
                "action": "subscription_upgrade",
                "entity": f"account:{account_id}",
                "target_plan": state.get("target_plan_code"),
                "amount": metadata.get("proration_amount"),
                "reasons": metadata.get("approval_reasons", []),
                "evidence": [e.get("source_ref") for e in state.get("evidence", [])],
            }
        )

        # The decision is *read*, never written here. Layer 2 of §13 puts
        # recording the human's decision — with actor and timestamp — in the
        # approval API, and that row is the authority. A second write path in
        # this node would let the graph's view and the audit record diverge,
        # and the divergence would favour whichever ran last.
        #
        # The resumed value is therefore only a signal that a decision exists;
        # what it *was* comes from the database.
        del decision

        async with deps.session_factory() as session:
            record = await session.get(Approval, approval_id)
            approved = record is not None and record.status == ApprovalStatus.APPROVED
            recorded_status = record.status if record is not None else "missing"

        return {
            "approval_id": approval_id,
            "approval_status": (ApprovalState.GRANTED if approved else ApprovalState.REJECTED),
            "status": WorkflowStatus.EXECUTING if approved else WorkflowStatus.ESCALATED,
            "escalation_reason": (
                None
                if approved
                else f"Approval was not granted (recorded status: {recorded_status})."
            ),
        }

    # ------------------------------------------------------------------- execute
    async def execute(state: WorkflowState) -> dict[str, Any]:
        """Apply the change through permission-checked, approval-gated tools.

        Three mutations in order — billing, CRM, then the legacy portal — all
        under **one** human approval. A person approves *the upgrade*, not three
        technical steps, so each tool verifies the same approval record and only
        the last consumes it. Spending it on the first would leave billing
        changed and provisioning refused: precisely the divergence §14 exists to
        catch, manufactured by the enforcement itself.

        Provisioning is last because it is the slowest and least reversible: a
        browser session that fails after billing and the CRM have been updated
        leaves a divergence the Validator will catch, whereas a portal flip
        followed by a failed billing update leaves one nothing points at.
        """
        account_id = state.get("account_id")
        target = state.get("target_plan_code")
        if account_id is None or not target:
            return _escalate_update("Execution needs an account and a target plan.")

        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        async with deps.session_factory() as session:
            context = _tool_context(session, state, Role.EXECUTION)

            subscription_id = (
                await session.execute(
                    select(Subscription.id).where(Subscription.account_id == account_id)
                )
            ).scalar_one_or_none()

            if subscription_id is None:
                await session.rollback()
                return _escalate_update(f"Account {account_id} has no subscription.")

            # The single authorisation every mutation in this workflow verifies
            # against, matching what the approval gate recorded.
            approval_scope = ("account", str(account_id))
            approval_action = "subscription_upgrade"

            plan_change = await execute_tool(
                context,
                ToolName.UPDATE_SUBSCRIPTION,
                UpdateSubscriptionInput(subscription_id=subscription_id, target_plan_code=target),
                handlers.update_subscription,
                approval_entity=approval_scope,
                approval_action=approval_action,
                consume_approval=False,
            )
            results.append(_execution_result("update_subscription", plan_change))
            if plan_change.error is not None:
                errors.append(_error("execute", plan_change.error))

            crm_sync = await execute_tool(
                context,
                ToolName.UPDATE_CRM,
                UpdateCrmInput(account_id=account_id, plan_code=target),
                handlers.update_crm,
                approval_entity=approval_scope,
                approval_action=approval_action,
                consume_approval=False,
            )
            results.append(_execution_result("update_crm", crm_sync))
            if crm_sync.error is not None:
                errors.append(_error("execute", crm_sync.error))

            # --- Provisioning: the browser step (§11, D8) -------------------
            if deps.provisioning is None:
                # Recorded as a failure rather than skipped: an unprovisioned
                # upgrade that reports success is the exact outcome this
                # architecture exists to prevent.
                results.append(
                    {
                        "step": "update_entitlement",
                        "tool": ToolName.UPDATE_ENTITLEMENT,
                        "ok": False,
                        "error_code": ToolErrorCode.PRECONDITION_FAILED,
                        "detail": {"reason": "No provisioning client is configured."},
                    }
                )
                errors.append(
                    {
                        "stage": "execute",
                        "code": ToolErrorCode.PRECONDITION_FAILED,
                        "message": "Entitlement provisioning is not configured.",
                        "retryable": False,
                    }
                )
            else:
                seats = (
                    await session.execute(
                        select(Subscription.seats).where(Subscription.id == subscription_id)
                    )
                ).scalar_one_or_none() or 1

                provisioned = await execute_tool(
                    context,
                    ToolName.UPDATE_ENTITLEMENT,
                    UpdateEntitlementInput(account_id=account_id, tier=target, seats=int(seats)),
                    handlers.make_update_entitlement(deps.provisioning),
                    approval_entity=approval_scope,
                    approval_action=approval_action,
                    # Last mutation: this one spends the approval.
                    consume_approval=True,
                )
                results.append(_execution_result("update_entitlement", provisioned))
                if provisioned.error is not None:
                    errors.append(_error("execute", provisioned.error))
                elif provisioned.data is not None and not provisioned.data.matches_request:
                    # The form submitted and the portal confirmed something else.
                    errors.append(
                        {
                            "stage": "execute",
                            "code": ToolErrorCode.PRECONDITION_FAILED,
                            "message": (
                                f"Portal confirmed '{provisioned.data.confirmed_tier}' "
                                f"but '{target}' was requested."
                            ),
                            "retryable": False,
                        }
                    )

            await session.commit()

        return {
            "execution_results": results,
            "errors": errors,
            "status": WorkflowStatus.VALIDATING,
        }

    # ------------------------------------------------------------------ validate
    async def validate(state: WorkflowState) -> dict[str, Any]:
        """Re-read every affected system and compare (§14).

        Reads from the systems of record — never from what execute returned.
        """
        account_id = state.get("account_id")
        target = state.get("target_plan_code")
        if account_id is None or not target:
            return {
                "validation_results": [],
                "status": WorkflowStatus.ESCALATED,
                "escalation_reason": "Nothing to validate.",
            }

        async with deps.session_factory() as session:
            context = _tool_context(session, state, Role.VALIDATOR)

            await record_event(
                session,
                EventType.VALIDATION_STARTED,
                actor_type=ActorType.AGENT,
                actor_id=Role.VALIDATOR,
                entity_type="account",
                entity_id=str(account_id),
                payload={"expected_plan_code": target},
                execution_id=state.get("execution_id"),
                request_id=state.get("request_id"),
            )

            billing = await execute_tool(
                context,
                ToolName.GET_SUBSCRIPTION,
                AccountInput(account_id=account_id),
                handlers.get_subscription,
            )
            account = await crm_service.get_account(session, account_id)

            # Read the entitlement from the **portal**, not from our mirror of
            # it. Querying the entitlements table would check our own side of
            # the integration, and a validator that validates itself proves
            # nothing (§14). None means the portal could not be consulted, which
            # the comparison reports as needing review rather than as agreement.
            entitlement_tier: str | None = None
            if deps.provisioning is not None:
                portal_read = await execute_tool(
                    context,
                    ToolName.GET_ENTITLEMENT,
                    AccountInput(account_id=account_id),
                    handlers.make_get_entitlement(deps.provisioning),
                )
                if portal_read.ok and portal_read.data is not None:
                    entitlement_tier = portal_read.data.tier

            observed = ObservedState(
                billing_plan_code=billing.data.plan_code if billing.ok and billing.data else None,
                billing_status=billing.data.status if billing.ok and billing.data else None,
                crm_plan_code=account.current_plan_code if account else None,
                entitlement_tier=entitlement_tier,
            )
            await session.commit()

        results = validate_upgrade(ExpectedState(plan_code=target), observed)
        verdict = overall_verdict(results)
        diverged = divergent_systems(results)

        # The cross-system verdict, and which systems disagreed. This is the
        # event that answers "was the outcome actually real?" (§14).
        await _emit(
            deps,
            state,
            EventType.VALIDATION_COMPLETED,
            actor_id=Role.VALIDATOR,
            entity_type="account",
            entity_id=str(account_id),
            payload={
                "verdict": str(verdict),
                "diverged_systems": sorted(diverged),
                "checks": len(results),
            },
        )

        update: dict[str, Any] = {"validation_results": results}
        if verdict != ValidationVerdict.PASS:
            update["errors"] = [
                {
                    "stage": "validate",
                    "code": "cross_system_divergence",
                    "message": (
                        f"Systems disagree after execution: {', '.join(diverged)}."
                        if diverged
                        else "Validation could not confirm the outcome."
                    ),
                    # A divergence is a state problem, not a transient fault.
                    "retryable": False,
                }
            ]
        return update

    # -------------------------------------------------------------------- notify
    async def notify(state: WorkflowState) -> dict[str, Any]:
        """Draft the confirmation and record the intent.

        **Delivery is not implemented.** BUILD_SPEC lists ``send_notification``
        as a tool, but nothing yet sends anything, and a node that reported
        "notified" without sending would be the exact failure the Validator
        exists to catch (Rule 6). The draft is recorded; the workflow says so.
        """
        draft = await deps.chat.structured(
            system=NOTIFIER_SYSTEM,
            user=(
                f"Confirm the upgrade to {state.get('target_plan_code')} for "
                f"{state.get('customer_ref')}."
            ),
            schema=NotificationDraft,
        )
        return {
            "execution_results": [
                {
                    "step": "notify",
                    "tool": "none",
                    "ok": True,
                    "error_code": None,
                    "detail": {
                        "subject": draft.subject,
                        "delivery": "not_implemented",
                        "note": "Draft recorded; no delivery mechanism exists yet.",
                    },
                }
            ],
            "status": WorkflowStatus.COMPLETED,
        }

    # ------------------------------------------------------------------ terminal
    async def escalate(state: WorkflowState) -> dict[str, Any]:
        reason = state.get("escalation_reason") or _infer_escalation_reason(state)
        logger.warning(
            "workflow_escalated",
            execution_id=str(state.get("execution_id")),
            reason=reason,
        )
        return {"status": WorkflowStatus.ESCALATED, "escalation_reason": reason}

    async def complete(state: WorkflowState) -> dict[str, Any]:
        logger.info("workflow_completed", execution_id=str(state.get("execution_id")))
        return {"status": WorkflowStatus.COMPLETED}

    return NodeSet(
        supervisor=supervisor,
        planner=planner,
        research=research,
        decide=decide,
        approval_gate=approval_gate,
        execute=execute,
        validate=validate,
        notify=notify,
        escalate=escalate,
        complete=complete,
    )


def _evidence_refs(assessment: assessment_module.UpgradeAssessment) -> list[str]:
    """Source references from an assessment, for attaching to a decision.

    Only the string-valued entries: the evidence dict also carries counts, which
    are facts about the assessment rather than pointers to where a fact came
    from.
    """
    return [value for value in assessment.evidence.values() if isinstance(value, str)]


def _execution_result(step: str, result: Any) -> dict[str, Any]:
    return {
        "step": step,
        "tool": step,
        "ok": bool(result.ok),
        "error_code": str(result.error.code) if result.error else None,
        "detail": result.data.model_dump(mode="json") if result.data is not None else {},
    }


def _error(stage: str, error: Any) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": str(error.code),
        "message": error.message,
        "retryable": bool(error.retryable),
    }


def _infer_escalation_reason(state: WorkflowState) -> str:
    """A reason a human can act on, even when no node set one."""
    errors = state.get("errors") or []
    if errors:
        return errors[-1]["message"]
    if not (state.get("metadata") or {}).get("evidence_sufficient", True):
        return "Retrieved evidence was insufficient to decide."
    if state.get("workflow_type") == WorkflowType.UNKNOWN:
        return "The request could not be classified."
    return "Escalated without a recorded reason."
