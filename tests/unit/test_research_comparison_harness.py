"""Is the measurement trustworthy?

§10 says the measurement is the deliverable, which makes the measuring code
load-bearing: a scoring bug would produce a confident number and a wrong ADR.
These tests cover the parts that could silently lie — completeness scoring,
ground-truth checking, degradation injection, and the percentile definition.

The two framework implementations are exercised too, but only to the depth that
matters here: that each actually reaches all five sources and survives one of
them failing. Their *relative* cost is what the benchmark run measures.
"""

from __future__ import annotations

import pytest
from benchmarks.research_comparison.harness import (
    ALL_SOURCES,
    INPUT_SET,
    SOURCE_LATENCY_MS,
    Meter,
    RunResult,
    SourceBank,
    SourceError,
    SourceName,
    expected_sources,
    percentile,
    verify_against_truth,
)
from benchmarks.research_comparison.langgraph_flow import run_langgraph

ACCOUNT = INPUT_SET[0]


# ------------------------------------------------------------ the sources


def test_every_input_supplies_every_source() -> None:
    """A missing fact would score as a framework failure rather than a gap here."""
    for account in INPUT_SET:
        assert set(account.facts) == set(ALL_SOURCES), account.account_id


def test_a_healthy_bank_returns_the_known_fact() -> None:
    bank = SourceBank()
    assert bank.fetch(ACCOUNT, SourceName.CONTRACT) == ACCOUNT.facts[SourceName.CONTRACT]


def test_the_degraded_source_actually_fails() -> None:
    """The degradation must be real, or the failure-mode scenario measures nothing."""
    bank = SourceBank(degraded=SourceName.POLICY)

    with pytest.raises(SourceError):
        bank.fetch(ACCOUNT, SourceName.POLICY)


def test_only_the_degraded_source_fails() -> None:
    bank = SourceBank(degraded=SourceName.POLICY)

    for source in ALL_SOURCES:
        if source is SourceName.POLICY:
            continue
        assert bank.fetch(ACCOUNT, source) == ACCOUNT.facts[source]


def test_reads_are_counted_so_a_skipped_source_is_visible() -> None:
    bank = SourceBank()
    bank.fetch(ACCOUNT, SourceName.PRICING)

    assert bank.reads == [SourceName.PRICING]


# --------------------------------------------------------- ground truth


def test_completeness_is_one_when_every_labelled_source_is_present() -> None:
    result = RunResult(
        account_id=ACCOUNT.account_id,
        evidence={str(s): ACCOUNT.facts[s] for s in ALL_SOURCES},
    )
    assert result.completeness(expected_sources(None)) == 1.0


def test_completeness_falls_when_a_source_is_missing() -> None:
    evidence = {str(s): ACCOUNT.facts[s] for s in ALL_SOURCES}
    del evidence[str(SourceName.SUPPORT)]

    result = RunResult(account_id=ACCOUNT.account_id, evidence=evidence)

    assert result.completeness(expected_sources(None)) == pytest.approx(0.8)


def test_a_degraded_source_is_excluded_from_the_ground_truth() -> None:
    """Otherwise every framework is penalised for a source nobody could read.

    The scenario measures how each one *copes*, not whether it can conjure a
    record from a system that is down.
    """
    expected = expected_sources(SourceName.POLICY)

    assert SourceName.POLICY not in expected
    assert len(expected) == len(ALL_SOURCES) - 1


def test_an_empty_value_does_not_count_as_evidence() -> None:
    """Reporting a source with nothing in it is not gathering it."""
    evidence = {str(s): ACCOUNT.facts[s] for s in ALL_SOURCES}
    evidence[str(SourceName.CONTRACT)] = ""

    result = RunResult(account_id=ACCOUNT.account_id, evidence=evidence)

    assert result.completeness(expected_sources(None)) == pytest.approx(0.8)


def test_a_wrong_fact_is_caught_even_at_full_completeness() -> None:
    """Completeness alone cannot tell 'gathered five' from 'gathered five badly'.

    This is what makes a fabricated answer visible in the results rather than
    scoring as a perfect run.
    """
    evidence = {str(s): ACCOUNT.facts[s] for s in ALL_SOURCES}
    evidence[str(SourceName.PRICING)] = "enterprise 999.00 USD per seat per month"

    result = RunResult(account_id=ACCOUNT.account_id, evidence=evidence)

    assert result.completeness(expected_sources(None)) == 1.0
    assert verify_against_truth(ACCOUNT, result) == ["'pricing' does not match the known fact"]


def test_an_invented_source_is_caught() -> None:
    result = RunResult(account_id=ACCOUNT.account_id, evidence={"astrology": "mercury retrograde"})

    assert verify_against_truth(ACCOUNT, result) == ["invented source 'astrology'"]


def test_a_correct_answer_has_no_truth_problems() -> None:
    result = RunResult(
        account_id=ACCOUNT.account_id,
        evidence={str(s): ACCOUNT.facts[s] for s in ALL_SOURCES},
    )
    assert verify_against_truth(ACCOUNT, result) == []


# ------------------------------------------------------------- statistics


def test_percentile_picks_a_real_observation() -> None:
    """Nearest-rank, so the reported figure is a run that actually happened."""
    values = [10.0, 20.0, 30.0, 40.0, 100.0]

    assert percentile(values, 0.50) == 30.0
    assert percentile(values, 0.95) == 100.0


def test_percentile_of_nothing_is_zero_rather_than_an_error() -> None:
    assert percentile([], 0.95) == 0.0


# ----------------------------------------------------------- the metering


def test_the_meter_counts_both_directions() -> None:
    meter = Meter()
    meter.record("12345678", "abcd")

    assert meter.calls == 1
    assert meter.tokens_in == 2
    assert meter.tokens_out == 1


def test_cost_rises_with_tokens() -> None:
    cheap, dear = Meter(), Meter()
    cheap.record("x" * 400, "y" * 40)
    dear.record("x" * 4000, "y" * 400)

    assert dear.estimated_cost_usd > cheap.estimated_cost_usd


# ---------------------------------------------- the LangGraph implementation


async def test_the_langgraph_fanout_gathers_every_source() -> None:
    bank = SourceBank()

    result = await run_langgraph(bank, ACCOUNT, Meter())

    assert set(result.evidence) == {str(s) for s in ALL_SOURCES}
    assert verify_against_truth(ACCOUNT, result) == []


async def test_the_langgraph_fanout_keeps_partial_evidence_when_a_source_fails() -> None:
    """A raised exception would abort the superstep and lose four good sources."""
    bank = SourceBank(degraded=SourceName.POLICY)

    result = await run_langgraph(bank, ACCOUNT, Meter())

    assert str(SourceName.POLICY) not in result.evidence
    assert len(result.evidence) == len(ALL_SOURCES) - 1
    assert result.errors
    assert result.completeness(expected_sources(SourceName.POLICY)) == 1.0


async def test_the_langgraph_fanout_overlaps_its_sources() -> None:
    """Fan-out must cost about the slowest source, not the sum of all five.

    If this ever fails, the graph has acquired an accidental edge and the
    'parallel' in the comparison is a fiction.
    """
    bank = SourceBank()

    result = await run_langgraph(bank, ACCOUNT, Meter())

    serial_ms = sum(SOURCE_LATENCY_MS[source] for source in ALL_SOURCES)

    assert result.elapsed_ms < serial_ms


async def test_the_langgraph_fanout_makes_exactly_one_model_call() -> None:
    """Retrieval is deterministic code; only synthesis consults a model."""
    meter = Meter()

    await run_langgraph(SourceBank(), ACCOUNT, meter)

    assert meter.calls == 1
