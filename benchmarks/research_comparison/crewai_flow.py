"""The CrewAI version: a crew of specialist researchers plus a synthesis task (§10).

Five researcher agents, one per source, each holding a tool that reads its own
source; then a synthesis agent whose task takes the five as ``context``. The
retrieval tasks are marked ``async_execution=True``, which is CrewAI's mechanism
for concurrency and the closest available analogue to the LangGraph fan-out.

**A deterministic model stands in for a real one**, subclassing ``BaseLLM`` so no
API key and no network are involved. This is what makes the comparison
repeatable, and it means the measured difference is framework scaffolding rather
than model variance.

Note what the structure forces: because CrewAI drives tool use *through the
model*, each researcher needs at least one model round trip to decide to call
its own single tool. The LangGraph version calls the same five sources with no
model involvement at all. That difference is the headline finding, and it is
structural rather than a tuning artefact.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

# Set before importing crewai: telemetry and tracing would add network calls to
# a latency benchmark and phone home about a private codebase.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from crewai import Agent, Crew, Process, Task
from crewai.llms.base_llm import BaseLLM
from crewai.tools import BaseTool
from crewai.utilities.types import LLMMessage
from pydantic import BaseModel, Field

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

_TOOL_CHOICE = re.compile(r"only one name of \[([^\]]+)\]")
_ACCOUNT_ID = re.compile(r"(acct-[a-z]+)")


class DeterministicLLM(BaseLLM):
    """A model that behaves like a competent one, deterministically.

    It emulates the minimum a real model would do inside CrewAI's ReAct loop:
    when the agent has a tool and has not yet observed a result, it emits an
    ``Action``; once the observation is back, it answers.

    An earlier version answered immediately without ever calling a tool. That
    scored CrewAI at 0% evidence completeness — a damning number that measured
    *the stub's* laziness, not the framework. Emulating one tool call is what
    makes the comparison about CrewAI rather than about my stand-in.

    Metered here rather than through ``Crew.usage_metrics`` because a custom LLM
    reports no usage to CrewAI's accounting, and because both frameworks must be
    counted by the same definition to be comparable at all.
    """

    def __init__(self, meter: Meter) -> None:
        super().__init__(model="deterministic/benchmark")
        self._meter = meter

    def call(
        self,
        messages: str | list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        callbacks: list[Any] | None = None,
        available_functions: dict[str, Any] | None = None,
        from_task: Any | None = None,
        from_agent: Any | None = None,
        response_model: Any | None = None,
    ) -> str:
        prompt = (
            messages
            if isinstance(messages, str)
            else "\n".join(str(message.get("content", "")) for message in messages)
        )
        self._meter.record(prompt, reply := self._reply(prompt))
        return reply

    def _reply(self, prompt: str) -> str:
        """One tool call, then a final answer.

        The transcript is whatever follows the framework's ``Begin!`` marker;
        the format instructions above it mention ``Observation:`` too, so
        searching the whole prompt would see a result that has not happened.
        """
        transcript = prompt.rsplit("Begin!", 1)[-1]
        choice = _TOOL_CHOICE.search(prompt)

        if choice is not None and "Observation:" not in transcript:
            tool = choice.group(1).split(",")[0].strip()
            account_id = match.group(1) if (match := _ACCOUNT_ID.search(prompt)) else ""
            return (
                "Thought: I should read my source.\n"
                f"Action: {tool}\n"
                f'Action Input: {{"account_id": "{account_id}"}}'
            )

        # CrewAI's parser treats this marker as the end of the turn. Without it
        # the agent iterates to max_iter, measuring the stub against the parser
        # rather than measuring the framework.
        return f"Thought: I now know the final answer\nFinal Answer: {SYNTHESIS_REPLY}"

    def supports_function_calling(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 8192


class _SourceArgs(BaseModel):
    """Typed tool arguments.

    A real tool has a schema, and CrewAI renders it into the prompt for the
    model to fill in. Leaving ``*args, **kwargs`` produced an unusable
    ``ForwardRef('Any')`` rendering that the agent could not satisfy, so the
    tool never ran — which would have measured my tool definition rather than
    the framework.
    """

    account_id: str = Field(default="", description="The account under review.")


def _make_tool(bank: SourceBank, account: Account, source: SourceName) -> BaseTool:
    """A tool that reads exactly one source.

    One tool per agent, mirroring the LangGraph node granularity so the two
    implementations are doing the same amount of work per unit.
    """

    class SourceTool(BaseTool):
        name: str = f"read_{source}"
        description: str = f"Read the {source} record for the account under review."
        args_schema: type[BaseModel] = _SourceArgs

        def _run(self, account_id: str = "") -> str:
            try:
                return bank.fetch(account, source)
            except SourceError as error:
                # Returned, not raised: a raised tool error aborts the task, and
                # the comparison is about what survives a degraded source.
                return f"ERROR: {error}"

    return SourceTool()


def build_crew(bank: SourceBank, account: Account, meter: Meter) -> tuple[Crew, dict[str, Any]]:
    """Assemble the researcher crew and the synthesis task."""
    llm = DeterministicLLM(meter)
    tools = {source: _make_tool(bank, account, source) for source in ALL_SOURCES}

    researchers = {
        source: Agent(
            role=f"{str(source).title()} Researcher",
            goal=f"Report the {source} facts for account {account.account_id}.",
            backstory=(
                f"You are a specialist who reads the {source} system of record "
                "and reports exactly what it says, without inference."
            ),
            tools=[tools[source]],
            llm=llm,
            verbose=False,
            allow_delegation=False,
            max_iter=2,
        )
        for source in ALL_SOURCES
    }

    retrieval_tasks = [
        Task(
            description=(
                f"Use your tool to read the {source} record for account "
                f"{account.account_id}. Report exactly what it returns."
            ),
            expected_output=f"The {source} facts, verbatim.",
            agent=researchers[source],
            # CrewAI's concurrency mechanism, and the nearest analogue to the
            # LangGraph fan-out.
            async_execution=True,
        )
        for source in ALL_SOURCES
    ]

    synthesiser = Agent(
        role="Evidence Synthesiser",
        goal="Combine the researchers' findings into one assessment-ready summary.",
        backstory="You assemble evidence for an upgrade decision.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
        max_iter=2,
    )
    synthesis_task = Task(
        description="Summarise the gathered evidence for an upgrade assessment.",
        expected_output="A short summary citing each source.",
        agent=synthesiser,
        context=retrieval_tasks,
    )

    crew = Crew(
        agents=[*researchers.values(), synthesiser],
        tasks=[*retrieval_tasks, synthesis_task],
        process=Process.sequential,
        verbose=False,
        memory=False,
        cache=False,
    )
    return crew, {"retrieval_tasks": retrieval_tasks}


def run_crewai(bank: SourceBank, account: Account, meter: Meter) -> RunResult:
    """Run one account through the crew and time it."""
    crew, parts = build_crew(bank, account, meter)

    started = time.perf_counter()
    crew.kickoff()
    elapsed_ms = (time.perf_counter() - started) * 1000

    # Evidence is recovered from what the tools actually returned, not from the
    # model's prose. Parsing the summary would measure the stub's writing rather
    # than the framework's retrieval, and would credit the crew for facts it
    # never read.
    evidence: dict[str, str] = {}
    errors: list[str] = []
    for index, source in enumerate(ALL_SOURCES):
        output = parts["retrieval_tasks"][index].output
        raw = str(output.raw) if output is not None else ""
        if source == bank.degraded:
            errors.append(f"Retrieval source '{source}' is unavailable.")
            continue
        # The tool ran if the source was read; take the fact from the bank's
        # own record rather than from the model's paraphrase of it.
        if source in bank.reads:
            evidence[str(source)] = account.facts[source]
        elif raw:
            errors.append(f"'{source}' produced output without reading its source")

    return RunResult(
        account_id=account.account_id,
        evidence=evidence,
        errors=errors,
        elapsed_ms=elapsed_ms,
    )
