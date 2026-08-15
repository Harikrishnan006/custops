"""Run both implementations over the same inputs and record the numbers.

    uv run python -m benchmarks.research_comparison.run

Writes ``benchmarks/results/research_comparison.json`` and a Markdown summary
that ADR-004 quotes. Re-running overwrites both, so the ADR's figures can always
be regenerated rather than taken on trust.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.research_comparison.crewai_flow import run_crewai
from benchmarks.research_comparison.harness import (
    IDEAL_PARALLEL_MS,
    IDEAL_SERIAL_MS,
    INPUT_SET,
    Account,
    Meter,
    RunResult,
    SourceBank,
    SourceName,
    expected_sources,
    summarise,
    verify_against_truth,
)
from benchmarks.research_comparison.langgraph_flow import run_langgraph

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"

# Enough runs that p95 is a measurement rather than "the worst of a handful".
# An earlier pass at 3 repetitions produced a p95 of 76ms against a p50 of 21ms
# on identical work — scheduler noise, not signal.
REPETITIONS = 5


@dataclass
class Observation:
    framework: str
    account_id: str
    degraded: str | None
    elapsed_ms: float
    completeness: float
    errors: list[str]
    truth_problems: list[str]


async def _one_run(
    framework: str, account: Account, degraded: SourceName | None, meter: Meter
) -> Observation:
    bank = SourceBank(degraded=degraded)
    if framework == "langgraph":
        result: RunResult = await run_langgraph(bank, account, meter)
    else:
        # CrewAI's kickoff is synchronous; run it off the event loop so the
        # await-based harness does not distort its timing.
        result = await asyncio.to_thread(run_crewai, bank, account, meter)

    return Observation(
        framework=framework,
        account_id=account.account_id,
        degraded=str(degraded) if degraded else None,
        elapsed_ms=round(result.elapsed_ms, 2),
        completeness=result.completeness(expected_sources(degraded)),
        errors=result.errors,
        truth_problems=verify_against_truth(account, result),
    )


async def _scenario(framework: str, degraded: SourceName | None) -> tuple[list[Observation], Meter]:
    """Every input, repeated, under one degradation condition."""
    # Warm up against a throwaway meter: first-call import, graph compilation and
    # Pydantic model building are one-off costs that would otherwise land
    # entirely in the first observation and distort both the mean and the p95.
    await _one_run(framework, INPUT_SET[0], degraded, Meter())

    meter = Meter()
    observations: list[Observation] = []
    for _ in range(REPETITIONS):
        for account in INPUT_SET:
            observations.append(await _one_run(framework, account, degraded, meter))
    return observations, meter


def _report(observations: list[Observation], meter: Meter) -> dict[str, Any]:
    latencies = [o.elapsed_ms for o in observations]
    completeness = [o.completeness for o in observations]
    runs = len(observations)
    return {
        "runs": runs,
        "latency_ms": summarise(latencies),
        "completeness_mean": round(sum(completeness) / runs, 4) if runs else 0.0,
        "completeness_min": round(min(completeness), 4) if runs else 0.0,
        "model_calls": meter.calls,
        "model_calls_per_run": round(meter.calls / runs, 2) if runs else 0.0,
        "chars_in": meter.chars_in,
        "chars_out": meter.chars_out,
        "tokens_in": meter.tokens_in,
        "tokens_out": meter.tokens_out,
        "estimated_cost_usd": float(round(meter.estimated_cost_usd, 6)),
        "estimated_cost_per_run_usd": float(round(meter.estimated_cost_usd / runs, 6))
        if runs
        else 0.0,
        "runs_with_errors": sum(1 for o in observations if o.errors),
        "runs_disagreeing_with_truth": sum(1 for o in observations if o.truth_problems),
    }


async def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scenarios: dict[str, SourceName | None] = {
        "healthy": None,
        # §10: simulate one retrieval source erroring. Policy is chosen because
        # it is the slowest source, so its loss is visible in latency as well as
        # in completeness.
        "degraded_policy": SourceName.POLICY,
    }

    results: dict[str, Any] = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "repetitions": REPETITIONS,
            "inputs": len(INPUT_SET),
        },
        # The two regimes a reader can compare each framework against: what
        # perfect overlap would cost, and what perfect serialisation would.
        # Without these, "81ms" is a number with nothing to mean anything against.
        "reference_ms": {
            "ideal_parallel": IDEAL_PARALLEL_MS,
            "ideal_serial": IDEAL_SERIAL_MS,
        },
        "scenarios": {},
    }

    for scenario_name, degraded in scenarios.items():
        scenario_block: dict[str, Any] = {}
        for framework in ("langgraph", "crewai"):
            observations, meter = await _scenario(framework, degraded)
            scenario_block[framework] = _report(observations, meter)
            report = scenario_block[framework]
            print(
                f"{scenario_name:>16} {framework:>10}  "
                f"p50={report['latency_ms']['p50']:>8.2f}ms  "
                f"p95={report['latency_ms']['p95']:>8.2f}ms  "
                f"calls/run={report['model_calls_per_run']:>5.2f}  "
                f"tokens_in={report['tokens_in']:>7}  "
                f"completeness={report['completeness_mean']:.2f}"
            )
        results["scenarios"][scenario_name] = scenario_block

    (RESULTS_DIR / "research_comparison.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    (RESULTS_DIR / "research_comparison.md").write_text(_markdown(results), encoding="utf-8")
    print(f"\nWrote {RESULTS_DIR / 'research_comparison.json'}")
    return 0


def _markdown(results: dict[str, Any]) -> str:
    lines = [
        "# Evidence-research subflow: LangGraph vs CrewAI",
        "",
        "Generated by `uv run python -m benchmarks.research_comparison.run`.",
        "",
        f"- Python {results['environment']['python']} on {results['environment']['platform']}",
        f"- {results['environment']['inputs']} inputs x "
        f"{results['environment']['repetitions']} repetitions per scenario",
        f"- Reference: perfect overlap would cost "
        f"{results['reference_ms']['ideal_parallel']:.0f}ms of source I/O; "
        f"perfect serialisation {results['reference_ms']['ideal_serial']:.0f}ms",
        "",
        "Latency is **framework overhead**, not end-to-end latency: the model is a",
        "deterministic offline stub, so a real model call's seconds are absent from",
        "both sides equally.",
        "",
    ]
    for scenario_name, block in results["scenarios"].items():
        lines += [
            f"## Scenario: {scenario_name}",
            "",
            "| Metric | LangGraph | CrewAI |",
            "|---|---:|---:|",
        ]
        rows: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
            ("Latency p50 (ms)", lambda r: f"{r['latency_ms']['p50']:.2f}"),
            ("Latency p95 (ms)", lambda r: f"{r['latency_ms']['p95']:.2f}"),
            ("Model calls per run", lambda r: f"{r['model_calls_per_run']:.2f}"),
            ("Tokens in (total)", lambda r: f"{r['tokens_in']:,}"),
            ("Tokens out (total)", lambda r: f"{r['tokens_out']:,}"),
            ("Est. cost per run (USD)", lambda r: f"{r['estimated_cost_per_run_usd']:.6f}"),
            ("Evidence completeness", lambda r: f"{r['completeness_mean']:.2f}"),
            ("Runs disagreeing with truth", lambda r: str(r["runs_disagreeing_with_truth"])),
        ]
        for label, render in rows:
            lines.append(f"| {label} | {render(block['langgraph'])} | {render(block['crewai'])} |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
