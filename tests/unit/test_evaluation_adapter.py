"""Does a CustOps execution map onto AgentForge's structures correctly?

This is the seam Phase 11 exists to build, and the place where a quiet mistake
would poison every metric downstream. AgentForge scores what the adapter hands
it; if the adapter emits a ``FINAL_ANSWER`` where the workflow actually refused,
every guardrail case scores as a success.

Particular attention to the six step types, and above all to **REFUSAL versus
ESCALATION** — both make ``AgentTrace.escalated`` true, so AgentForge cannot
tell them apart and will not complain if the adapter confuses them.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_forge.models import StepType

from custops.agents.state import WorkflowStatus
from custops.evaluation.adapter import ExecutionRecord, to_agent_trace, tool_sequence_of
from custops.evaluation.datasets.scenarios import (
    SCENARIOS_BY_ID,
    SyntheticEvent,
    SyntheticExecution,
    SyntheticStep,
    SyntheticToolCall,
)
from custops.observability.events import EventType


def _record(
    *,
    status: str = WorkflowStatus.COMPLETED,
    steps: list[Any] | None = None,
    tool_calls: list[Any] | None = None,
    events: list[Any] | None = None,
    escalation_reason: str | None = None,
    final_state: dict[str, Any] | None = None,
) -> ExecutionRecord:
    return ExecutionRecord(
        execution=SyntheticExecution(
            id=uuid.uuid4(),
            raw_request="Upgrade ACME to enterprise.",
            status=status,
            escalation_reason=escalation_reason,
            final_state=final_state or {},
        ),
        steps=steps or [],
        tool_calls=tool_calls or [],
        events=events or [],
    )


def _types(trace: Any) -> list[StepType]:
    return [step.type for step in trace.steps]


# ------------------------------------------------------------ the six types


def test_a_tool_call_becomes_a_call_and_a_result() -> None:
    """AgentForge models the request and the response as separate steps."""
    trace = to_agent_trace(
        _record(tool_calls=[SyntheticToolCall("get_subscription", result={"plan_code": "pro"})]),
        task_id="t",
    )

    assert _types(trace)[:2] == [StepType.TOOL_CALL, StepType.TOOL_RESULT]
    assert trace.steps[0].tool_name == "get_subscription"
    assert trace.steps[1].tool_result is not None


def test_tool_arguments_are_carried_onto_the_call_step() -> None:
    trace = to_agent_trace(
        _record(tool_calls=[SyntheticToolCall("get_invoice", arguments={"account_id": "a-1"})]),
        task_id="t",
    )

    assert trace.steps[0].tool_args == {"account_id": "a-1"}


def test_a_failed_tool_result_says_so() -> None:
    """A trace that renders failures as successes would score a broken run
    as a clean one."""
    trace = to_agent_trace(
        _record(
            tool_calls=[SyntheticToolCall("update_crm", succeeded=False, error_code="conflict")]
        ),
        task_id="t",
    )

    assert "ERROR" in (trace.steps[1].tool_result or "")
    assert "conflict" in (trace.steps[1].tool_result or "")


def test_a_deciding_node_becomes_a_reasoning_step() -> None:
    trace = to_agent_trace(
        _record(
            steps=[
                SyntheticStep(
                    0,
                    "decide",
                    {"decisions": [{"name": "upgrade_eligibility", "outcome": "eligible"}]},
                )
            ]
        ),
        task_id="t",
    )

    assert StepType.REASONING in _types(trace)
    assert "upgrade_eligibility=eligible" in trace.steps[0].content


def test_a_completed_run_ends_in_a_final_answer() -> None:
    trace = to_agent_trace(
        _record(
            steps=[
                SyntheticStep(
                    0, "notify", {"notification": {"subject": "Done", "body": "Upgraded."}}
                )
            ]
        ),
        task_id="t",
    )

    assert _types(trace)[-1] == StepType.FINAL_ANSWER
    assert trace.final_answer is not None
    assert "Upgraded" in trace.final_answer


# ------------------------------------------------- refusal versus escalation


def test_a_policy_block_is_a_refusal() -> None:
    """Eligibility said no. The system worked exactly as designed."""
    trace = to_agent_trace(
        _record(
            status=WorkflowStatus.ESCALATED,
            events=[
                SyntheticEvent(
                    str(EventType.DECISION_MADE),
                    {"outcome": "blocked", "blockers": ["account_not_active"]},
                )
            ],
            escalation_reason="Upgrade blocked: account_not_active.",
        ),
        task_id="t",
    )

    assert _types(trace)[-1] == StepType.REFUSAL


def test_a_human_rejection_is_a_refusal_not_a_failure() -> None:
    """An approver declining is the approval system working."""
    trace = to_agent_trace(
        _record(
            status=WorkflowStatus.ESCALATED,
            events=[SyntheticEvent(str(EventType.APPROVAL_RECEIVED), {"approved": False})],
            escalation_reason="Approval rejected.",
        ),
        task_id="t",
    )

    assert _types(trace)[-1] == StepType.REFUSAL


def test_a_human_approval_is_not_a_refusal() -> None:
    """The inverse, so the check is reading the flag and not merely the event."""
    trace = to_agent_trace(
        _record(
            status=WorkflowStatus.ESCALATED,
            events=[SyntheticEvent(str(EventType.APPROVAL_RECEIVED), {"approved": True})],
            escalation_reason="Validation failed after approval.",
        ),
        task_id="t",
    )

    assert _types(trace)[-1] == StepType.ESCALATION


def test_a_tool_failure_is_an_escalation() -> None:
    """Nothing declined anything; the platform could not finish."""
    trace = to_agent_trace(
        _record(
            status=WorkflowStatus.ESCALATED,
            tool_calls=[
                SyntheticToolCall("update_subscription", succeeded=False, error_code="timeout")
            ],
            escalation_reason="Billing update timed out.",
        ),
        task_id="t",
    )

    assert _types(trace)[-1] == StepType.ESCALATION


def test_a_failed_status_is_terminal_too() -> None:
    trace = to_agent_trace(
        _record(status=WorkflowStatus.FAILED, escalation_reason="Unhandled error."),
        task_id="t",
    )

    assert _types(trace)[-1] == StepType.ESCALATION


def test_both_terminal_kinds_count_as_escalated_for_agentforge() -> None:
    """The property AgentForge scores guardrail tasks on.

    The adapter's distinction is for CustOps' own evaluators and for humans;
    it must not change how AgentForge sees a refusal.
    """
    refusal = to_agent_trace(
        _record(
            status=WorkflowStatus.ESCALATED,
            events=[SyntheticEvent(str(EventType.DECISION_MADE), {"outcome": "blocked"})],
        ),
        task_id="t",
    )
    escalation = to_agent_trace(
        _record(status=WorkflowStatus.ESCALATED, escalation_reason="Tool failed."), task_id="t"
    )

    assert refusal.escalated is True
    assert escalation.escalated is True


def test_a_terminal_run_does_not_also_emit_a_final_answer() -> None:
    """A run that both refused and answered would be incoherent, and would let
    a guardrail case score as a completed task."""
    trace = to_agent_trace(
        _record(
            status=WorkflowStatus.ESCALATED,
            events=[SyntheticEvent(str(EventType.DECISION_MADE), {"outcome": "blocked"})],
        ),
        task_id="t",
    )

    assert StepType.FINAL_ANSWER not in _types(trace)


# ------------------------------------------------------------- Rule 18


def test_chain_of_thought_never_reaches_a_reasoning_step() -> None:
    """The prohibition follows the data across the boundary.

    A reasoning step is the one place a careless adapter would put
    deliberation, so it is redacted exactly as an audit payload is.
    """
    trace = to_agent_trace(
        _record(
            steps=[
                SyntheticStep(
                    0,
                    "decide",
                    {
                        "reasoning": "LEAKED DELIBERATION",
                        "decisions": [{"name": "x", "outcome": "eligible"}],
                    },
                )
            ]
        ),
        task_id="t",
    )

    assert "LEAKED" not in trace.steps[0].content


def test_secrets_in_tool_arguments_are_masked_in_the_trace() -> None:
    trace = to_agent_trace(
        _record(
            tool_calls=[SyntheticToolCall("update_entitlement", arguments={"password": "s3cret"})]
        ),
        task_id="t",
    )

    assert "s3cret" not in str(trace.steps[0].tool_args)


# ---------------------------------------------------------------- ordering


def test_the_tool_sequence_preserves_call_order() -> None:
    """``tool_sequence`` is what AgentForge compares to ``expected_tools``, so
    order is load-bearing rather than cosmetic."""
    record = _record(
        tool_calls=[
            SyntheticToolCall("get_customer"),
            SyntheticToolCall("get_subscription"),
            SyntheticToolCall("update_subscription"),
        ]
    )

    trace = to_agent_trace(record, task_id="t")

    assert trace.tool_sequence == ["get_customer", "get_subscription", "update_subscription"]
    assert tool_sequence_of(record) == trace.tool_sequence


def test_steps_are_numbered_from_one_without_gaps() -> None:
    trace = to_agent_trace(
        _record(
            steps=[SyntheticStep(0, "decide", {})],
            tool_calls=[SyntheticToolCall("get_customer")],
        ),
        task_id="t",
    )

    assert [step.step for step in trace.steps] == list(range(1, len(trace.steps) + 1))


def test_latency_sums_tool_time_rather_than_wall_clock() -> None:
    """A run paused at the approval gate for two days must not report a
    two-day latency: that measures the human, not the platform."""
    trace = to_agent_trace(
        _record(
            tool_calls=[
                SyntheticToolCall("get_customer", duration_ms=10),
                SyntheticToolCall("get_subscription", duration_ms=15),
            ]
        ),
        task_id="t",
    )

    assert trace.latency_ms == 25


def test_the_original_request_is_carried_as_the_task() -> None:
    trace = to_agent_trace(_record(), task_id="upgrade-happy-path")

    assert trace.task_id == "upgrade-happy-path"
    assert trace.task == "Upgrade ACME to enterprise."


# ------------------------------------------------ the real dataset scenarios


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("upgrade-happy-path", StepType.FINAL_ANSWER),
        ("inactive-account", StepType.REFUSAL),
        ("contract-restriction", StepType.REFUSAL),
        ("approval-rejection", StepType.REFUSAL),
        ("discount-above-threshold", StepType.REFUSAL),
        ("billing-api-timeout", StepType.ESCALATION),
        ("crm-update-failure", StepType.ESCALATION),
        ("browser-failure", StepType.ESCALATION),
        ("entitlement-billing-divergence", StepType.ESCALATION),
        ("low-retrieval-confidence", StepType.ESCALATION),
        ("malformed-contract-document", StepType.ESCALATION),
        ("customer-not-found", StepType.ESCALATION),
    ],
)
def test_each_scenario_terminates_in_the_right_kind(task_id: str, expected: StepType) -> None:
    """Pins the refusal/escalation split across the whole §15 dataset.

    Note ``customer-not-found`` is an escalation, not a refusal: nothing
    declined the request on policy grounds — the platform could not find the
    account it was asked about.
    """
    trace = to_agent_trace(SCENARIOS_BY_ID[task_id].record, task_id=task_id)

    assert trace.steps[-1].type is expected
