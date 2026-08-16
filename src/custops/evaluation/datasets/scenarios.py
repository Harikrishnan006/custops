"""Synthetic CustOps executions, one per golden task (§15).

**These are CustOps rows, not AgentForge traces.** Each scenario declares what
Phase 12 would have recorded — the execution, its graph steps, its tool calls
and its audit events — and the evaluation runner feeds them through the real
adapter. Hand-authoring ``AgentTrace`` objects instead would make every
evaluation pass whether or not the adapter worked.

§15 requires synthetic adversarial coverage, and names the cases: customer not
found, inactive account, contract restriction, discount above threshold, billing
API timeout, CRM update failure, entitlement/billing divergence, malformed
contract document, low retrieval confidence, approval rejection, browser
failure. All eleven are here, plus two paths that should succeed — an evaluation
set containing only failures cannot detect a platform that refuses everything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from custops.agents.state import WorkflowStatus
from custops.evaluation.adapter import ExecutionRecord
from custops.observability.events import EventType

# Stable ids so a scenario's identity does not change between runs — a
# regression comparison across versions depends on comparing like with like.
_NAMESPACE = uuid.UUID("6f3d1d1e-2a5b-4c9f-8e21-000000000011")


def _execution_id(task_id: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, task_id)


@dataclass(frozen=True, slots=True)
class SyntheticExecution:
    """Stands in for a ``WorkflowExecution`` row."""

    id: uuid.UUID
    raw_request: str
    status: str
    escalation_reason: str | None = None
    retry_count: int = 0
    replan_count: int = 0
    final_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SyntheticStep:
    """Stands in for a ``WorkflowStep`` row."""

    sequence: int
    node: str
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SyntheticToolCall:
    """Stands in for a ``ToolCall`` row."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    succeeded: bool = True
    error_code: str | None = None
    duration_ms: int = 12


@dataclass(frozen=True, slots=True)
class SyntheticEvent:
    """Stands in for an ``AuditEvent`` row."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LabelledScenario:
    """A synthetic execution plus the labels only a dataset can supply.

    ``expected_evidence`` and ``expected_plan`` are ground truth for the
    CustOps-specific evaluators — retrieval precision/recall and planning
    accuracy — which is exactly the kind of thing a generic harness cannot know.
    """

    task_id: str
    record: ExecutionRecord
    expected_plan: tuple[str, ...] = ()
    expected_evidence: frozenset[str] = frozenset()
    # For the divergence case: was a cross-system disagreement injected, and did
    # the Validator have to catch it?
    divergence_injected: bool = False
    divergence_caught: bool = False


def _decide_step(sequence: int, outcome: str, summary: str, status: str) -> SyntheticStep:
    return SyntheticStep(
        sequence=sequence,
        node="decide",
        output={
            "status": status,
            "decisions": [
                {
                    "name": "upgrade_eligibility",
                    "outcome": outcome,
                    "rationale_summary": summary,
                }
            ],
        },
    )


def _reads(*names: str) -> list[SyntheticToolCall]:
    return [SyntheticToolCall(tool_name=name, result={"ok": True}) for name in names]


def _scenario(
    task_id: str,
    raw_request: str,
    *,
    status: str,
    steps: list[SyntheticStep],
    tool_calls: list[SyntheticToolCall],
    events: list[SyntheticEvent],
    escalation_reason: str | None = None,
    final_state: dict[str, Any] | None = None,
    expected_plan: tuple[str, ...] = (),
    expected_evidence: frozenset[str] = frozenset(),
    divergence_injected: bool = False,
    divergence_caught: bool = False,
) -> LabelledScenario:
    return LabelledScenario(
        task_id=task_id,
        record=ExecutionRecord(
            execution=SyntheticExecution(
                id=_execution_id(task_id),
                raw_request=raw_request,
                status=status,
                escalation_reason=escalation_reason,
                final_state=final_state or {},
            ),
            steps=list(steps),
            tool_calls=list(tool_calls),
            events=list(events),
        ),
        expected_plan=expected_plan,
        expected_evidence=expected_evidence,
        divergence_injected=divergence_injected,
        divergence_caught=divergence_caught,
    )


def _notify_step(sequence: int, text: str) -> SyntheticStep:
    return SyntheticStep(
        sequence=sequence,
        node="notify",
        output={"notification": {"subject": text, "body": text}},
    )


_BLOCKED = str(EventType.DECISION_MADE)
_APPROVAL = str(EventType.APPROVAL_RECEIVED)
_VALIDATION = str(EventType.VALIDATION_COMPLETED)

_STANDARD_PLAN = ("research", "decide", "execute", "validate")


def build_scenarios() -> list[LabelledScenario]:
    """Every §15 case, as CustOps records."""
    scenarios: list[LabelledScenario] = []

    # ------------------------------------------------------------- succeeds
    scenarios.append(
        _scenario(
            "upgrade-happy-path",
            "Upgrade ACME to the enterprise plan.",
            status=WorkflowStatus.COMPLETED,
            steps=[
                SyntheticStep(0, "supervisor", {"status": "planning"}),
                SyntheticStep(1, "planner", {"status": "researching"}),
                SyntheticStep(2, "research", {"status": "deciding"}),
                _decide_step(3, "eligible", "All checks passed.", "executing"),
                SyntheticStep(4, "validate", {"status": "completed"}),
                _notify_step(
                    5,
                    "Subscription upgraded to enterprise; confirmed across billing, "
                    "crm and entitlement.",
                ),
            ],
            tool_calls=_reads(
                "get_customer",
                "get_subscription",
                "get_invoice",
                "get_support_history",
                "search_knowledge",
                "update_subscription",
                "update_crm",
                "update_entitlement",
                "get_entitlement",
            ),
            events=[SyntheticEvent(_VALIDATION, {"verdict": "pass", "diverged_systems": []})],
            expected_plan=_STANDARD_PLAN,
            expected_evidence=frozenset({"account", "subscription", "invoice", "support"}),
        )
    )

    scenarios.append(
        _scenario(
            "upgrade-requires-approval",
            "Upgrade UMBRELLA to enterprise; the amount exceeds the approval threshold.",
            status=WorkflowStatus.COMPLETED,
            steps=[
                SyntheticStep(0, "supervisor", {"status": "planning"}),
                SyntheticStep(1, "research", {"status": "deciding"}),
                _decide_step(2, "eligible", "Approval required by amount.", "awaiting_approval"),
                SyntheticStep(3, "validate", {"status": "completed"}),
                _notify_step(
                    4,
                    "Subscription upgraded to enterprise after approval; confirmed "
                    "across billing, crm and entitlement.",
                ),
            ],
            tool_calls=_reads(
                "get_customer",
                "get_subscription",
                "get_invoice",
                "search_knowledge",
                "update_subscription",
                "update_crm",
                "update_entitlement",
                "get_entitlement",
            ),
            events=[
                SyntheticEvent(_APPROVAL, {"approved": True}),
                SyntheticEvent(_VALIDATION, {"verdict": "pass", "diverged_systems": []}),
            ],
            expected_plan=_STANDARD_PLAN,
            expected_evidence=frozenset({"account", "subscription", "invoice"}),
        )
    )

    # ------------------------------------------------- refusals (policy said no)
    scenarios.append(
        _scenario(
            "customer-not-found",
            "Upgrade NOSUCHCO to enterprise.",
            status=WorkflowStatus.ESCALATED,
            steps=[SyntheticStep(0, "supervisor", {"status": "researching"})],
            tool_calls=[
                SyntheticToolCall("get_customer", succeeded=False, error_code="not_found")
            ],
            events=[],
            escalation_reason="No account found for customer 'NOSUCHCO'.",
            expected_plan=("research",),
        )
    )

    scenarios.append(
        _scenario(
            "inactive-account",
            "Upgrade a churned customer to enterprise.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "blocked", "Blocked by: account_not_active.", "escalated"),
            ],
            tool_calls=_reads("get_customer", "get_subscription"),
            events=[
                SyntheticEvent(_BLOCKED, {"outcome": "blocked", "blockers": ["account_not_active"]})
            ],
            escalation_reason="Upgrade blocked: account_not_active.",
            expected_plan=("research", "decide"),
            expected_evidence=frozenset({"account", "subscription"}),
        )
    )

    scenarios.append(
        _scenario(
            "contract-restriction",
            "Upgrade VEHEMENT to enterprise despite a contract term lock.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "blocked", "Blocked by: contract_term_locked.", "escalated"),
            ],
            tool_calls=_reads(
                "get_customer", "get_subscription", "get_contract", "search_knowledge"
            ),
            events=[
                SyntheticEvent(
                    _BLOCKED, {"outcome": "blocked", "blockers": ["contract_term_locked"]}
                )
            ],
            escalation_reason="Upgrade blocked: contract_term_locked.",
            expected_plan=("research", "decide"),
            expected_evidence=frozenset({"account", "subscription", "contract", "policy"}),
        )
    )

    scenarios.append(
        _scenario(
            "discount-above-threshold",
            "Upgrade a heavily discounted account to enterprise.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "blocked", "Discount exceeds policy threshold.", "escalated"),
            ],
            tool_calls=_reads(
                "get_customer", "get_subscription", "get_invoice", "search_knowledge"
            ),
            events=[
                SyntheticEvent(
                    _BLOCKED, {"outcome": "blocked", "blockers": ["discount_above_threshold"]}
                )
            ],
            escalation_reason="Discount above threshold requires review.",
            expected_plan=("research", "decide"),
            expected_evidence=frozenset({"account", "subscription", "invoice", "policy"}),
        )
    )

    scenarios.append(
        _scenario(
            "approval-rejection",
            "Upgrade HOOLI to enterprise where the approver declines.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "eligible", "Approval required.", "awaiting_approval"),
            ],
            tool_calls=_reads(
                "get_customer", "get_subscription", "get_invoice", "search_knowledge"
            ),
            # A human said no. That is a refusal, not a malfunction.
            events=[SyntheticEvent(_APPROVAL, {"approved": False})],
            escalation_reason="Approval rejected by reviewer.",
            expected_plan=("research", "decide"),
            expected_evidence=frozenset({"account", "subscription", "invoice"}),
        )
    )

    # ----------------------------------------- escalations (something went wrong)
    scenarios.append(
        _scenario(
            "billing-api-timeout",
            "Upgrade ACME to enterprise while the billing system is timing out.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "eligible", "All checks passed.", "executing"),
            ],
            tool_calls=[
                *_reads("get_customer", "get_subscription"),
                SyntheticToolCall(
                    "update_subscription", succeeded=False, error_code="timeout", duration_ms=5000
                ),
            ],
            events=[],
            escalation_reason="Billing update timed out.",
            expected_plan=_STANDARD_PLAN,
            expected_evidence=frozenset({"account", "subscription"}),
        )
    )

    scenarios.append(
        _scenario(
            "crm-update-failure",
            "Upgrade ACME to enterprise while the CRM rejects the write.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "eligible", "All checks passed.", "executing"),
            ],
            tool_calls=[
                *_reads("get_customer", "get_subscription", "update_subscription"),
                SyntheticToolCall("update_crm", succeeded=False, error_code="conflict"),
            ],
            events=[],
            escalation_reason="CRM rejected the plan change.",
            expected_plan=_STANDARD_PLAN,
            expected_evidence=frozenset({"account", "subscription"}),
        )
    )

    scenarios.append(
        _scenario(
            "entitlement-billing-divergence",
            "Upgrade ACME to enterprise where the portal provisions the wrong tier.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "eligible", "All checks passed.", "executing"),
                SyntheticStep(2, "validate", {"status": "escalated"}),
            ],
            tool_calls=_reads(
                "get_customer",
                "get_subscription",
                "update_subscription",
                "update_crm",
                "update_entitlement",
                "get_entitlement",
            ),
            # The case D8 exists for: every step reported success, the portal
            # provisioned something else, and validation caught it anyway.
            events=[
                SyntheticEvent(
                    _VALIDATION, {"verdict": "fail", "diverged_systems": ["entitlement"]}
                )
            ],
            escalation_reason="Billing and entitlement disagree.",
            expected_plan=_STANDARD_PLAN,
            expected_evidence=frozenset({"account", "subscription"}),
            divergence_injected=True,
            divergence_caught=True,
        )
    )

    scenarios.append(
        _scenario(
            "malformed-contract-document",
            "Upgrade an account whose contract document cannot be parsed.",
            status=WorkflowStatus.ESCALATED,
            steps=[SyntheticStep(0, "research", {"status": "escalated"})],
            tool_calls=[
                *_reads("get_customer", "get_subscription"),
                SyntheticToolCall("get_contract", succeeded=False, error_code="invalid_document"),
                SyntheticToolCall("search_knowledge", result={"sufficient": False}),
            ],
            events=[],
            escalation_reason="Contract terms could not be established.",
            expected_plan=("research",),
            expected_evidence=frozenset({"account", "subscription"}),
        )
    )

    scenarios.append(
        _scenario(
            "low-retrieval-confidence",
            "Upgrade an account where retrieval returns weak evidence.",
            status=WorkflowStatus.ESCALATED,
            steps=[SyntheticStep(0, "research", {"status": "escalated"})],
            tool_calls=[
                *_reads("get_customer", "get_subscription"),
                SyntheticToolCall("search_knowledge", result={"sufficient": False}),
            ],
            events=[],
            escalation_reason="Retrieved evidence was insufficient.",
            expected_plan=("research",),
            expected_evidence=frozenset({"account", "subscription", "policy"}),
        )
    )

    scenarios.append(
        _scenario(
            "browser-failure",
            "Upgrade ACME to enterprise while the legacy portal browser fails to launch.",
            status=WorkflowStatus.ESCALATED,
            steps=[
                SyntheticStep(0, "research", {"status": "deciding"}),
                _decide_step(1, "eligible", "All checks passed.", "executing"),
            ],
            tool_calls=[
                *_reads("get_customer", "get_subscription", "update_subscription", "update_crm"),
                SyntheticToolCall(
                    "update_entitlement", succeeded=False, error_code="browser_unavailable"
                ),
            ],
            events=[],
            escalation_reason="Chromium could not be launched for the legacy portal.",
            expected_plan=_STANDARD_PLAN,
            expected_evidence=frozenset({"account", "subscription"}),
        )
    )

    return scenarios


SCENARIOS: list[LabelledScenario] = build_scenarios()
SCENARIOS_BY_ID: dict[str, LabelledScenario] = {s.task_id: s for s in SCENARIOS}
