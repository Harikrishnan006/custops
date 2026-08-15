"""The CrewAI implementation, checked for the things that would void the result.

A comparison is only evidence if the losing side was actually given a fair run.
Two specific ways this benchmark could have lied about CrewAI:

* **The stub never calls a tool.** An earlier version answered immediately, and
  CrewAI scored 0% evidence completeness — a number that measured my stand-in
  model's laziness and would have produced a confidently wrong ADR.
* **The crew is scored on the model's prose** rather than on what its tools
  returned, which with a deterministic stub would score zero for reasons that
  have nothing to do with the framework.

These tests pin both down. They are slower than the rest of the unit suite
because a crew genuinely runs; that is the point.
"""

from __future__ import annotations

from benchmarks.research_comparison.crewai_flow import DeterministicLLM, run_crewai
from benchmarks.research_comparison.harness import (
    ALL_SOURCES,
    INPUT_SET,
    Meter,
    SourceBank,
    SourceName,
    expected_sources,
    verify_against_truth,
)

ACCOUNT = INPUT_SET[0]

# The shape CrewAI renders into the agent prompt. Reproduced here so the stub's
# behaviour can be tested without running a crew.
_PROMPT = (
    "You ONLY have access to the following tools...\n"
    "Action: the action to take, only one name of [read_contract], just the name.\n"
    "Current Task: read the contract record for account acct-acme.\n"
    "Begin! This is VERY important to you.\n\nThought:"
)


def test_the_stub_calls_its_tool_before_answering() -> None:
    """A model that never uses its tool would frame CrewAI for a failure of mine."""
    reply = DeterministicLLM(Meter())._reply(_PROMPT)

    assert "Action: read_contract" in reply
    assert "Final Answer" not in reply


def test_the_stub_passes_the_account_id_the_tool_schema_expects() -> None:
    reply = DeterministicLLM(Meter())._reply(_PROMPT)

    assert '"account_id": "acct-acme"' in reply


def test_the_stub_answers_once_the_observation_is_back() -> None:
    """Otherwise the agent loops to max_iter and the run measures the parser."""
    observed = _PROMPT + "\nAction: read_contract\nObservation: CTR-1001 active\nThought:"

    reply = DeterministicLLM(Meter())._reply(observed)

    assert "Final Answer" in reply
    assert "Action:" not in reply


def test_the_stub_is_not_fooled_by_the_format_instructions() -> None:
    """The instructions above ``Begin!`` mention ``Observation:`` themselves.

    Searching the whole prompt would make the stub answer without ever calling a
    tool — the exact bug that produced a bogus 0% score.
    """
    instructions_only = (
        "Action: the action to take, only one name of [read_contract].\n"
        "Observation: the result of the action\n"
        "Begin!\n\nThought:"
    )

    reply = DeterministicLLM(Meter())._reply(instructions_only)

    assert "Action: read_contract" in reply


def test_the_crew_actually_reads_every_source() -> None:
    """Tools must run. This is the check that caught the 0% artefact."""
    bank = SourceBank()

    result = run_crewai(bank, ACCOUNT, Meter())

    assert set(bank.reads) == set(ALL_SOURCES)
    assert set(result.evidence) == {str(source) for source in ALL_SOURCES}
    assert verify_against_truth(ACCOUNT, result) == []


def test_the_crew_survives_a_degraded_source() -> None:
    bank = SourceBank(degraded=SourceName.POLICY)

    result = run_crewai(bank, ACCOUNT, Meter())

    assert str(SourceName.POLICY) not in result.evidence
    assert result.errors
    assert result.completeness(expected_sources(SourceName.POLICY)) == 1.0


def test_the_crew_costs_more_model_calls_than_it_has_tasks() -> None:
    """The headline finding, pinned as a test.

    CrewAI drives tool use *through* the model, so each researcher needs a round
    trip to decide to call its own single tool. If this ever stops being true the
    ADR's central measurement has changed and should be re-run.
    """
    meter = Meter()

    run_crewai(SourceBank(), ACCOUNT, meter)

    # Six tasks (five retrieval, one synthesis); more calls than tasks means the
    # framework is paying for the decision as well as the work.
    assert meter.calls > 6
