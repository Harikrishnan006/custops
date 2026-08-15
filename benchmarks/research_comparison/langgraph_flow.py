"""The LangGraph version: parallel node fan-out plus a synthesis step (§10).

Five retrieval nodes with no edges between them, so LangGraph schedules them
concurrently, then a synthesis node that joins. State is a TypedDict with
reducer-annotated fields, which is how the concurrent branches merge without
overwriting each other.

The single model call happens in synthesis. Retrieval is deterministic code —
the same division of labour the production graph uses, where the model
classifies and drafts and never decides.
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from benchmarks.research_comparison.harness import (
    ALL_SOURCES,
    SYNTHESIS_REPLY,
    Account,
    Meter,
    RunResult,
    SourceBank,
    SourceError,
    SourceName,
)


class ResearchState(TypedDict, total=False):
    """Fan-out state.

    ``evidence`` and ``errors`` carry reducers because five nodes write them
    concurrently. Without the reducer, LangGraph raises on the concurrent
    update rather than silently losing one — which is itself worth noting in
    the comparison: the failure is loud and at the framework level.
    """

    account_id: str
    evidence: Annotated[dict[str, str], operator.or_]
    errors: Annotated[list[str], operator.add]
    summary: str


def build_research_graph(bank: SourceBank, account: Account, meter: Meter) -> Any:
    """Compile the fan-out graph for one account."""
    graph: StateGraph[ResearchState, None, ResearchState, ResearchState] = StateGraph(ResearchState)

    def make_retriever(source: SourceName) -> Any:
        async def retrieve(state: ResearchState) -> dict[str, Any]:
            """One source. Failure is data, not an exception.

            A raised exception here would abort the whole superstep and lose the
            four sources that succeeded. Returning the error keeps partial
            evidence, which is what makes graceful degradation possible.
            """
            try:
                value = await bank.afetch(account, source)
            except SourceError as error:
                return {"errors": [str(error)]}
            return {"evidence": {str(source): value}}

        return retrieve

    for source in ALL_SOURCES:
        graph.add_node(f"retrieve_{source}", make_retriever(source))

    async def synthesise(state: ResearchState) -> dict[str, Any]:
        """The one model call, over evidence that is already gathered."""
        evidence = state.get("evidence", {})
        prompt = "Summarise the following evidence for an upgrade assessment.\n" + "\n".join(
            f"{name}: {value}" for name, value in sorted(evidence.items())
        )
        meter.record(prompt, SYNTHESIS_REPLY)
        return {"summary": SYNTHESIS_REPLY}

    graph.add_node("synthesise", synthesise)

    # No edges between retrievers: START fans out to all five, and all five fan
    # in to synthesis. LangGraph runs a superstep per level, so the five
    # overlap and the level costs max(source latency), not the sum.
    for source in ALL_SOURCES:
        graph.add_edge(START, f"retrieve_{source}")
        graph.add_edge(f"retrieve_{source}", "synthesise")
    graph.add_edge("synthesise", END)

    return graph.compile()


async def run_langgraph(bank: SourceBank, account: Account, meter: Meter) -> RunResult:
    """Run one account through the fan-out graph and time it."""
    compiled = build_research_graph(bank, account, meter)

    started = time.perf_counter()
    final = await compiled.ainvoke({"account_id": account.account_id, "evidence": {}, "errors": []})
    elapsed_ms = (time.perf_counter() - started) * 1000

    return RunResult(
        account_id=account.account_id,
        evidence=dict(final.get("evidence", {})),
        errors=list(final.get("errors", [])),
        elapsed_ms=elapsed_ms,
    )
