"""The assembled graph actually runs the §7 topology.

Driven with recording stub nodes and an in-memory checkpointer, so every path —
including each failure edge — is exercised with no database, no model and no
API key. What is verified is the *wiring*: that routing decisions reach the
nodes they name, and that budgets bound the loops.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from custops.agents.budgets import BudgetPolicy
from custops.agents.state import (
    ApprovalState,
    ValidationVerdict,
    WorkflowState,
    WorkflowStatus,
    WorkflowType,
    initial_state,
)
from custops.apps.orchestrator.checkpointer import CheckpointerError, open_checkpointer
from custops.apps.orchestrator.graph import Node, NodeSet, build_graph, compile_graph
from custops.config import Settings


class Recorder:
    """Stub nodes that record visits and apply a scripted state update."""

    def __init__(self) -> None:
        self.visited: list[str] = []
        self.updates: dict[str, dict[str, Any]] = {}

    def node(self, name: str) -> Node:
        async def run(state: WorkflowState) -> dict[str, Any]:
            self.visited.append(name)
            return dict(self.updates.get(name, {}))

        return run

    def script(self, name: str, **update: Any) -> None:
        self.updates[name] = update

    def node_set(self) -> NodeSet:
        return NodeSet(
            supervisor=self.node("supervisor"),
            planner=self.node("planner"),
            research=self.node("research"),
            decide=self.node("decide"),
            approval_gate=self.node("approval_gate"),
            execute=self.node("execute"),
            validate=self.node("validate"),
            notify=self.node("notify"),
            escalate=self.node("escalate"),
            complete=self.node("complete"),
        )


def _start() -> WorkflowState:
    return initial_state(
        execution_id=uuid.uuid4(),
        request_id="req-1",
        raw_request="Upgrade Acme to Enterprise.",
        started_at=datetime(2026, 8, 15, tzinfo=UTC),
    )


def _passing_validation() -> list[dict[str, str]]:
    return [
        {
            "check": "subscription_plan",
            "system": "billing",
            "verdict": ValidationVerdict.PASS,
            "expected": "enterprise",
            "actual": "enterprise",
        }
    ]


def _failing_validation() -> list[dict[str, str]]:
    return [
        {
            "check": "subscription_plan",
            "system": "billing",
            "verdict": ValidationVerdict.FAIL,
            "expected": "enterprise",
            "actual": "professional",
        }
    ]


async def _run(recorder: Recorder, policy: BudgetPolicy | None = None) -> WorkflowState:
    app = compile_graph(recorder.node_set(), checkpointer=InMemorySaver(), budget_policy=policy)
    config: RunnableConfig = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await app.ainvoke(_start(), config=config)
    return cast("WorkflowState", result)


class TestHappyPath:
    async def test_auto_approved_run_reaches_complete(self) -> None:
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "policy:UPG-001"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.NOT_REQUIRED)
        recorder.script("validate", validation_results=_passing_validation())
        recorder.script("complete", status=WorkflowStatus.COMPLETED)

        final = await _run(recorder)

        assert recorder.visited == [
            "supervisor",
            "planner",
            "research",
            "decide",
            "execute",
            "validate",
            "notify",
            "complete",
        ]
        assert final["status"] == WorkflowStatus.COMPLETED
        assert "escalate" not in recorder.visited


class TestEscalationPaths:
    async def test_unclassified_request_escalates_without_planning(self) -> None:
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.UNKNOWN)

        await _run(recorder)

        assert recorder.visited == ["supervisor", "escalate"]

    async def test_insufficient_evidence_escalates_without_deciding(self) -> None:
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "policy:UPG-001"}],
            metadata={"evidence_sufficient": False},
        )

        await _run(recorder)

        assert recorder.visited == ["supervisor", "planner", "research", "escalate"]
        assert "decide" not in recorder.visited


class TestApprovalGate:
    async def test_required_approval_routes_through_the_gate(self) -> None:
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "p"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.REQUIRED)
        recorder.script("approval_gate", approval_status=ApprovalState.GRANTED)
        recorder.script("validate", validation_results=_passing_validation())

        await _run(recorder)

        assert "approval_gate" in recorder.visited
        assert recorder.visited.index("approval_gate") < recorder.visited.index("execute")

    async def test_a_rejected_approval_never_reaches_execute(self) -> None:
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "p"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.REQUIRED)
        recorder.script("approval_gate", approval_status=ApprovalState.REJECTED)

        await _run(recorder)

        assert "execute" not in recorder.visited
        assert recorder.visited[-1] == "escalate"


class TestRecoveryLoops:
    async def test_a_transient_failure_retries_execute_then_escalates(self) -> None:
        """The retry loop must terminate — an unbounded one is the real risk."""
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "p"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.NOT_REQUIRED)
        # Always fails, always transient: only the budget can stop this.
        recorder.script(
            "validate",
            validation_results=_failing_validation(),
            errors=[
                {
                    "stage": "execute",
                    "code": "upstream_timeout",
                    "message": "",
                    "retryable": True,
                }
            ],
        )

        await _run(recorder, policy=BudgetPolicy(max_retries=0, max_replans=0))

        assert recorder.visited[-1] == "escalate"
        assert recorder.visited.count("execute") == 1

    async def test_a_non_retryable_failure_replans_rather_than_retrying(self) -> None:
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "p"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.NOT_REQUIRED)
        recorder.script(
            "validate",
            validation_results=_failing_validation(),
            errors=[
                {
                    "stage": "execute",
                    "code": "permission_denied",
                    "message": "",
                    "retryable": False,
                }
            ],
        )

        await _run(recorder, policy=BudgetPolicy(max_retries=2, max_replans=1))

        # Planned once, replanned once, then escalated. The loop terminates
        # because the graph spends the replan budget itself.
        assert recorder.visited.count("planner") == 2
        assert recorder.visited[-1] == "escalate"

    async def test_the_recovery_loop_terminates_even_if_nodes_never_increment(
        self,
    ) -> None:
        """Termination must not depend on node discipline.

        These stub nodes never touch retry_count or replan_count. If budget
        bookkeeping lived in the execute and planner nodes, this would loop
        until LangGraph's recursion limit.
        """
        recorder = Recorder()
        recorder.script("supervisor", workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE)
        recorder.script(
            "research",
            evidence=[{"source_ref": "p"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.NOT_REQUIRED)
        recorder.script(
            "validate",
            validation_results=_failing_validation(),
            errors=[
                {
                    "stage": "execute",
                    "code": "upstream_timeout",
                    "message": "",
                    "retryable": True,
                }
            ],
        )

        final = await _run(recorder, policy=BudgetPolicy(max_retries=2, max_replans=1))

        assert recorder.visited[-1] == "escalate"
        # Budgets were actually spent by the graph.
        assert final["replan_count"] == 1


class TestStateAccumulation:
    async def test_evidence_from_multiple_nodes_accumulates(self) -> None:
        """The reducer must append, not overwrite."""
        recorder = Recorder()
        recorder.script(
            "supervisor",
            workflow_type=WorkflowType.SUBSCRIPTION_UPGRADE,
            evidence=[{"source_ref": "a"}],
        )
        recorder.script(
            "research",
            evidence=[{"source_ref": "b"}],
            metadata={"evidence_sufficient": True},
        )
        recorder.script("decide", approval_status=ApprovalState.NOT_REQUIRED)
        recorder.script("validate", validation_results=_passing_validation())

        final = await _run(recorder)

        refs = [item["source_ref"] for item in final["evidence"]]
        assert refs == ["a", "b"]


class TestAssembly:
    def test_graph_builds_without_a_checkpointer(self) -> None:
        """Compiling must not require persistence — only pausing does."""
        recorder = Recorder()

        app = compile_graph(recorder.node_set())

        assert app is not None

    def test_every_node_in_the_topology_is_registered(self) -> None:
        recorder = Recorder()

        graph = build_graph(recorder.node_set())

        assert {
            "supervisor",
            "planner",
            "research",
            "decide",
            "approval_gate",
            "execute",
            "validate",
            "notify",
            "escalate",
            "complete",
        } <= set(graph.nodes)


class TestCheckpointerGuard:
    async def test_in_memory_checkpointer_is_refused_outside_local_and_test(self) -> None:
        """A run that cannot survive a restart is not human-in-the-loop."""
        settings = Settings(_env_file=None, environment="production")

        with pytest.raises(CheckpointerError, match="not permitted in environment"):
            async with open_checkpointer(settings, in_memory=True):
                pass

    async def test_in_memory_checkpointer_is_allowed_in_test(self) -> None:
        settings = Settings(_env_file=None, environment="test")

        async with open_checkpointer(settings, in_memory=True) as saver:
            assert saver is not None
