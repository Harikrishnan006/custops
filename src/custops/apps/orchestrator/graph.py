"""Graph assembly (BUILD_SPEC §7).

Built against LangGraph 1.2's real API: ``StateGraph(WorkflowState)``,
``add_conditional_edges(source, path, path_map)``, and
``compile(checkpointer=...)``.

**The graph is wiring, not logic.** Every conditional edge delegates to a pure
function in ``agents.routing``; nothing here decides anything. That separation is
what makes the topology testable without a runtime, and it is also the honest
reading of §7: the graph is a happy path, and the safety properties live in the
deterministic rules and the tool layer.

Node behaviour is supplied by a ``NodeSet`` rather than imported directly. The
graph therefore depends on an interface, and Phase 6 supplies the implementation
that talks to MCP tools and the enterprise services. That is not indirection for
its own sake — it is what lets the whole topology be exercised, including every
failure edge, without a database or a model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from custops.agents.budgets import BudgetPolicy
from custops.agents.routing import (
    APPROVAL_GATE,
    COMPLETE,
    DECIDE,
    ESCALATE,
    EXECUTE,
    NOTIFY,
    PLANNER,
    RESEARCH,
    SUPERVISOR,
    VALIDATE,
    route_after_approval,
    route_after_decide,
    route_after_research,
    route_after_supervisor,
    route_after_validate,
)
from custops.agents.state import WorkflowState

# A node receives the whole state and returns a *partial* update, which
# LangGraph merges through the reducers. Typing the return as the full state
# would misdescribe every node in the system.
Node = Callable[[WorkflowState], Awaitable[dict[str, Any]]]

# Graph-owned bookkeeping nodes. Not part of NodeSet: they carry no business
# behaviour and must not be substitutable, because the termination guarantee
# depends on them running exactly as written.
RETRY = "retry"
REPLAN = "replan"


async def _spend_retry(state: WorkflowState) -> dict[str, Any]:
    """Consume one retry before re-executing."""
    return {"retry_count": state.get("retry_count", 0) + 1}


async def _spend_replan(state: WorkflowState) -> dict[str, Any]:
    """Consume one replan before re-planning.

    Also resets the retry count: a new plan gets its own retry budget, because
    the failures that exhausted the previous one were against a plan that no
    longer applies.
    """
    return {"replan_count": state.get("replan_count", 0) + 1, "retry_count": 0}


@dataclass(frozen=True, slots=True)
class NodeSet:
    """The behaviour behind each node.

    One callable per node, each taking state and returning a partial update.
    Supplying these from outside keeps the graph free of infrastructure and lets
    a test drive any path deterministically.
    """

    supervisor: Node
    planner: Node
    research: Node
    decide: Node
    approval_gate: Node
    execute: Node
    validate: Node
    notify: Node
    escalate: Node
    complete: Node


def build_graph(
    nodes: NodeSet,
    *,
    budget_policy: BudgetPolicy | None = None,
) -> StateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
    """Assemble the §7 topology.

    Returns the uncompiled graph so the caller chooses the checkpointer —
    persistence is a deployment concern, not a topology one.
    """
    graph: StateGraph[WorkflowState, None, WorkflowState, WorkflowState] = StateGraph(WorkflowState)

    def register(name: str, node: Node) -> None:
        """Attach a node.

        The ignore is deliberate and confined to this one line. LangGraph's
        ``add_node`` overloads describe a node as returning the full state type
        or a ``Command``; ours return a *partial* update dict, which is what the
        reducers merge and what every node in this system actually produces.
        The call is correct at runtime — the overloads simply do not model it.

        The ignore is unqualified on purpose. mypy reports this as
        ``call-overload`` or ``arg-type`` depending on which it reaches first,
        and that shifts with unrelated edits elsewhere in the module graph — it
        has been observed as each. It also checks *unused* ignores per code, so
        naming any subset is unstable in both directions. Naming one is what
        first broke CI: it passed on a warm cache and failed on a cold one.
        """
        graph.add_node(name, node)  # type: ignore

    register(SUPERVISOR, nodes.supervisor)
    register(PLANNER, nodes.planner)
    register(RESEARCH, nodes.research)
    register(DECIDE, nodes.decide)
    register(APPROVAL_GATE, nodes.approval_gate)
    register(EXECUTE, nodes.execute)
    register(VALIDATE, nodes.validate)
    register(NOTIFY, nodes.notify)
    register(ESCALATE, nodes.escalate)
    register(COMPLETE, nodes.complete)

    graph.add_edge(START, SUPERVISOR)

    # Unclassifiable requests escalate rather than defaulting into the one
    # workflow that exists.
    graph.add_conditional_edges(
        SUPERVISOR, route_after_supervisor, {PLANNER: PLANNER, ESCALATE: ESCALATE}
    )
    graph.add_edge(PLANNER, RESEARCH)

    # Low retrieval confidence escalates; the model does not get to judge
    # whether its own evidence was enough.
    graph.add_conditional_edges(
        RESEARCH, route_after_research, {DECIDE: DECIDE, ESCALATE: ESCALATE}
    )
    # A decision that refused the request escalates without executing. Omitting
    # this edge does not make the refusal safe — it makes it unroutable, and the
    # run proceeds to execute a change it has already declined.
    graph.add_conditional_edges(
        DECIDE,
        route_after_decide,
        {APPROVAL_GATE: APPROVAL_GATE, EXECUTE: EXECUTE, ESCALATE: ESCALATE},
    )
    graph.add_conditional_edges(
        APPROVAL_GATE, route_after_approval, {EXECUTE: EXECUTE, ESCALATE: ESCALATE}
    )
    graph.add_edge(EXECUTE, VALIDATE)

    # The retry/replan/escalate fan-out. Budgets are enforced in Python; the
    # policy is bound here so a deployment can tighten it without touching the
    # routing rule.
    def _validate_router(state: WorkflowState) -> str:
        outcome = route_after_validate(state, policy=budget_policy)
        # Recovery goes via a counter node, so the budget is spent before the
        # work is repeated (see below).
        if outcome == EXECUTE:
            return RETRY
        if outcome == PLANNER:
            return REPLAN
        return outcome

    graph.add_conditional_edges(
        VALIDATE,
        _validate_router,
        {
            NOTIFY: NOTIFY,
            RETRY: RETRY,
            REPLAN: REPLAN,
            ESCALATE: ESCALATE,
        },
    )

    # Budget bookkeeping is owned by the graph, not by node implementations.
    #
    # This matters more than it looks. If incrementing were left to the execute
    # and planner nodes, a node that forgot would produce an unbounded loop —
    # precisely the failure budgets exist to prevent, reintroduced by the code
    # meant to be bounded. Spending the budget in a node the graph controls
    # makes termination a property of the topology: the loop shortens on every
    # pass no matter what the surrounding nodes do.
    graph.add_node(RETRY, _spend_retry)
    graph.add_node(REPLAN, _spend_replan)
    graph.add_edge(RETRY, EXECUTE)
    graph.add_edge(REPLAN, PLANNER)

    graph.add_edge(NOTIFY, COMPLETE)
    graph.add_edge(COMPLETE, END)
    # Escalation is terminal for the graph: a human takes over from here.
    graph.add_edge(ESCALATE, END)

    return graph


def compile_graph(
    nodes: NodeSet,
    *,
    checkpointer: object | None = None,
    budget_policy: BudgetPolicy | None = None,
) -> CompiledStateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
    """Compile the graph, optionally with a checkpointer.

    Without one the graph still runs — it simply cannot be paused and resumed,
    which is why anything using the approval gate must supply one.
    """
    graph = build_graph(nodes, budget_policy=budget_policy)
    return graph.compile(checkpointer=checkpointer)  # type: ignore[arg-type]
