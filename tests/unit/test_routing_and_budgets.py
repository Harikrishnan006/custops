"""Graph routing and recovery budgets.

These are the decisions BUILD_SPEC §7 and §12 insist stay in Python. Because
routing is a pure function of state, the whole topology is verifiable here with
no LangGraph runtime, no database and no model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from custops.agents.budgets import (
    BudgetOutcome,
    BudgetPolicy,
    next_recovery_step,
)
from custops.agents.routing import (
    APPROVAL_GATE,
    DECIDE,
    ESCALATE,
    EXECUTE,
    NOTIFY,
    PLANNER,
    route_after_approval,
    route_after_decide,
    route_after_research,
    route_after_supervisor,
    route_after_validate,
)
from custops.agents.state import (
    ApprovalState,
    ValidationVerdict,
    WorkflowState,
    WorkflowStatus,
    WorkflowType,
    initial_state,
)


def _state(**overrides: Any) -> WorkflowState:
    state = initial_state(
        execution_id=uuid.uuid4(),
        request_id="req-1",
        raw_request="Upgrade Acme to Enterprise.",
        started_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def _validation(verdict: str, check: str = "subscription_plan") -> dict[str, str]:
    return {
        "check": check,
        "system": "billing",
        "verdict": verdict,
        "expected": "enterprise",
        "actual": "enterprise" if verdict == ValidationVerdict.PASS else "professional",
    }


class TestSupervisorRouting:
    def test_a_classified_request_plans(self) -> None:
        assert (
            route_after_supervisor(_state(workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE))
            == PLANNER
        )

    def test_an_unclassified_request_escalates(self) -> None:
        """'I don't understand this' must never become 'upgrading your plan'."""
        assert route_after_supervisor(_state(workflow_type=WorkflowType.UNKNOWN)) == ESCALATE

    def test_an_unrecognised_type_escalates(self) -> None:
        assert route_after_supervisor(_state(workflow_type="billing_dispute")) == ESCALATE


class TestResearchRouting:
    def test_sufficient_evidence_decides(self) -> None:
        state = _state(
            evidence=[{"source_ref": "policy:UPG-001"}],
            metadata={"evidence_sufficient": True},
        )

        assert route_after_research(state) == DECIDE

    def test_insufficient_evidence_escalates(self) -> None:
        state = _state(
            evidence=[{"source_ref": "policy:UPG-001"}],
            metadata={"evidence_sufficient": False},
        )

        assert route_after_research(state) == ESCALATE

    def test_no_evidence_escalates(self) -> None:
        assert route_after_research(_state(evidence=[])) == ESCALATE

    def test_missing_sufficiency_verdict_escalates(self) -> None:
        """Absent a verdict, the safe reading is 'not sufficient'."""
        state = _state(evidence=[{"source_ref": "x"}], metadata={})

        assert route_after_research(state) == ESCALATE


class TestDecisionRouting:
    def test_auto_path_executes(self) -> None:
        assert route_after_decide(_state(approval_status=ApprovalState.NOT_REQUIRED)) == EXECUTE

    def test_approval_required_stops_at_the_gate(self) -> None:
        assert route_after_decide(_state(approval_status=ApprovalState.REQUIRED)) == APPROVAL_GATE

    def test_high_confidence_cannot_skip_the_gate(self) -> None:
        """Confidence feeds whether approval is needed; it never waives it."""
        state = _state(
            approval_status=ApprovalState.REQUIRED,
            decisions=[
                {
                    "name": "eligibility",
                    "outcome": "eligible",
                    "confidence": 1.0,
                    "rationale_summary": "All checks passed.",
                    "evidence_refs": [],
                    "decided_at": "2026-08-15T00:00:00Z",
                }
            ],
        )

        assert route_after_decide(state) == APPROVAL_GATE

    def test_a_refused_decision_escalates_without_executing(self) -> None:
        """The case the graph could not express until it had this edge.

        `decide` marks a blocked upgrade ESCALATED, but routing only ever chose
        between the approval gate and execution — so a term-locked contract was
        decided "blocked" and then executed anyway. The golden dataset's
        `contract-restriction` scenario is explicit that the run ends at
        `decide` with read-only tool calls.
        """
        state = _state(
            status=WorkflowStatus.ESCALATED,
            escalation_reason="Upgrade blocked: contract_term_locked.",
        )

        assert route_after_decide(state) == ESCALATE

    def test_a_refused_decision_escalates_even_when_approval_is_not_required(self) -> None:
        """`decide`'s assessment-error path sets exactly this combination.

        It marks the run ESCALATED *and* sets approval to NOT_REQUIRED, so a
        rule keyed on approval alone would send a failed assessment straight
        into execution — the refusal must win.
        """
        state = _state(
            status=WorkflowStatus.ESCALATED,
            approval_status=ApprovalState.NOT_REQUIRED,
            escalation_reason="Pricing could not be assessed.",
        )

        assert route_after_decide(state) == ESCALATE

    def test_a_refused_decision_outranks_a_pending_approval(self) -> None:
        """Refusal is terminal; asking a human to approve it would be theatre."""
        state = _state(
            status=WorkflowStatus.ESCALATED,
            approval_status=ApprovalState.REQUIRED,
            escalation_reason="Upgrade blocked: outstanding_past_due_invoices.",
        )

        assert route_after_decide(state) == ESCALATE

    def test_an_ordinary_decision_is_unaffected(self) -> None:
        """The escalate branch must not swallow the ordinary paths."""
        assert route_after_decide(_state(approval_status=ApprovalState.NOT_REQUIRED)) == EXECUTE
        assert route_after_decide(_state(approval_status=ApprovalState.REQUIRED)) == APPROVAL_GATE


class TestApprovalRouting:
    def test_granted_proceeds(self) -> None:
        assert route_after_approval(_state(approval_status=ApprovalState.GRANTED)) == EXECUTE

    @pytest.mark.parametrize(
        "status",
        [
            ApprovalState.REJECTED,
            ApprovalState.REQUIRED,
            ApprovalState.NOT_REQUIRED,
            "anything_else",
        ],
    )
    def test_anything_other_than_granted_escalates(self, status: str) -> None:
        """Mirrors the tool layer's exact-match rule: 'not rejected' is not 'approved'."""
        assert route_after_approval(_state(approval_status=status)) == ESCALATE


class TestValidationRouting:
    def test_all_pass_notifies(self) -> None:
        state = _state(
            validation_results=[
                _validation(ValidationVerdict.PASS, "subscription_plan"),
                _validation(ValidationVerdict.PASS, "entitlement_tier"),
            ]
        )

        assert route_after_validate(state) == NOTIFY

    def test_one_failure_among_passes_does_not_notify(self) -> None:
        """PASS only if *all* agree (§14)."""
        state = _state(
            validation_results=[
                _validation(ValidationVerdict.PASS, "subscription_plan"),
                _validation(ValidationVerdict.FAIL, "entitlement_tier"),
            ],
            errors=[
                {"stage": "execute", "code": "upstream_timeout", "message": "", "retryable": True}
            ],
        )

        assert route_after_validate(state) != NOTIFY

    def test_no_validation_results_escalates(self) -> None:
        """A validator that produced nothing has not validated anything."""
        assert route_after_validate(_state(validation_results=[])) == ESCALATE

    def test_needs_review_escalates_rather_than_retrying(self) -> None:
        """Repeating an action whose effect is unknown is how one upgrade becomes two."""
        state = _state(
            validation_results=[_validation(ValidationVerdict.NEEDS_REVIEW)],
            errors=[
                {"stage": "execute", "code": "upstream_timeout", "message": "", "retryable": True}
            ],
        )

        assert route_after_validate(state) == ESCALATE

    def test_transient_failure_retries_within_budget(self) -> None:
        state = _state(
            validation_results=[_validation(ValidationVerdict.FAIL)],
            errors=[
                {"stage": "execute", "code": "upstream_timeout", "message": "", "retryable": True}
            ],
            retry_count=0,
        )

        assert route_after_validate(state) == EXECUTE

    def test_exhausted_retries_replan(self) -> None:
        state = _state(
            validation_results=[_validation(ValidationVerdict.FAIL)],
            errors=[
                {"stage": "execute", "code": "upstream_timeout", "message": "", "retryable": True}
            ],
            retry_count=2,
            replan_count=0,
        )

        assert route_after_validate(state) == PLANNER

    def test_exhausted_budgets_escalate(self) -> None:
        state = _state(
            validation_results=[_validation(ValidationVerdict.FAIL)],
            errors=[
                {"stage": "execute", "code": "upstream_timeout", "message": "", "retryable": True}
            ],
            retry_count=2,
            replan_count=1,
        )

        assert route_after_validate(state) == ESCALATE

    def test_non_retryable_failure_skips_retry_and_replans(self) -> None:
        """Retrying a permission denial cannot succeed; it only burns budget."""
        state = _state(
            validation_results=[_validation(ValidationVerdict.FAIL)],
            errors=[
                {"stage": "execute", "code": "permission_denied", "message": "", "retryable": False}
            ],
            retry_count=0,
            replan_count=0,
        )

        assert route_after_validate(state) == PLANNER

    def test_failure_with_no_recorded_error_is_treated_as_non_retryable(self) -> None:
        """Silence is not evidence of a transient fault."""
        state = _state(
            validation_results=[_validation(ValidationVerdict.FAIL)],
            errors=[],
            retry_count=0,
        )

        assert route_after_validate(state) == PLANNER

    def test_budget_policy_is_injectable(self) -> None:
        strict = BudgetPolicy(max_retries=0, max_replans=0)
        state = _state(
            validation_results=[_validation(ValidationVerdict.FAIL)],
            errors=[
                {"stage": "execute", "code": "upstream_timeout", "message": "", "retryable": True}
            ],
        )

        assert route_after_validate(state, policy=strict) == ESCALATE


class TestBudgets:
    def test_transient_failure_retries_first(self) -> None:
        decision = next_recovery_step(retry_count=0, replan_count=0, failure_is_retryable=True)

        assert decision.outcome == BudgetOutcome.RETRY
        assert "1 of 2" in decision.reason

    def test_retries_are_bounded(self) -> None:
        decision = next_recovery_step(retry_count=2, replan_count=0, failure_is_retryable=True)

        assert decision.outcome == BudgetOutcome.REPLAN

    def test_replans_are_bounded(self) -> None:
        decision = next_recovery_step(retry_count=2, replan_count=1, failure_is_retryable=True)

        assert decision.outcome == BudgetOutcome.ESCALATE
        assert "escalating to a human" in decision.reason

    def test_non_retryable_never_retries(self) -> None:
        decision = next_recovery_step(retry_count=0, replan_count=0, failure_is_retryable=False)

        assert decision.outcome == BudgetOutcome.REPLAN

    def test_reason_is_always_populated(self) -> None:
        """Every recovery decision must be explainable in the audit trail."""
        for retries, replans, retryable in [
            (0, 0, True),
            (2, 0, True),
            (2, 1, True),
            (0, 0, False),
        ]:
            decision = next_recovery_step(
                retry_count=retries, replan_count=replans, failure_is_retryable=retryable
            )
            assert decision.reason

    def test_zero_budget_policy_escalates_immediately(self) -> None:
        decision = next_recovery_step(
            retry_count=0,
            replan_count=0,
            failure_is_retryable=True,
            policy=BudgetPolicy(max_retries=0, max_replans=0),
        )

        assert decision.outcome == BudgetOutcome.ESCALATE

    def test_decisions_are_reproducible(self) -> None:
        first = next_recovery_step(retry_count=1, replan_count=0, failure_is_retryable=True)
        second = next_recovery_step(retry_count=1, replan_count=0, failure_is_retryable=True)

        assert first == second


class TestStateShape:
    def test_initial_state_has_every_accumulator_as_an_empty_list(self) -> None:
        """A node reading evidence before any exists must get [], not a KeyError."""
        state = initial_state(
            execution_id=uuid.uuid4(),
            request_id="r",
            raw_request="x",
            started_at=datetime(2026, 8, 15, tzinfo=UTC),
        )

        for field in (
            "evidence",
            "decisions",
            "tool_calls",
            "execution_results",
            "validation_results",
            "errors",
        ):
            assert state[field] == []

    def test_budgets_start_at_zero(self) -> None:
        state = initial_state(
            execution_id=uuid.uuid4(),
            request_id="r",
            raw_request="x",
            started_at=datetime(2026, 8, 15, tzinfo=UTC),
        )

        assert state["retry_count"] == 0
        assert state["replan_count"] == 0
        assert state["approval_status"] == ApprovalState.NOT_REQUIRED
