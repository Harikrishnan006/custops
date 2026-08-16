"""``custops evaluate`` — the CI gate (§15).

Exits non-zero when AgentForge's regression rules find a regression, so a
quality drop fails the build rather than being noticed later. Deterministic by
construction: the judge is never used here, because a gate that fails when an
API call times out is worse than no gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from custops.evaluation.runner import (
    GATED_METRICS,
    EvaluationResult,
    compare_with_baseline,
    evaluate,
    failure_modes,
    load_baseline,
    write_baseline,
)

DEFAULT_BASELINE = Path("evaluation/baseline/trace_baseline.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custops evaluate",
        description="Score the orchestrator against the §15 datasets and gate on regressions.",
    )
    parser.add_argument("--version", required=True, help="version label, e.g. a git sha")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="committed baseline run to compare against",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="overwrite the baseline with this run instead of comparing",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="write the report as JSON")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    current, scores = evaluate(version=args.version, persist=False)

    print(f"Evaluated {len(scores)} scenarios  version={args.version}\n")
    print("Gated metrics (AgentForge regression rules)")
    print("-------------------------------------------")
    for key in sorted(GATED_METRICS):
        if key in current.summary:
            print(f"  {key:<34} {current.summary[key]}")

    print("\nReport-only metrics")
    print("-------------------")
    for key, value in sorted(current.summary.items()):
        if key not in GATED_METRICS:
            print(f"  {key:<34} {value}")

    breakdown = failure_modes(scores)
    if breakdown:
        print("\nFailure modes")
        print("-------------")
        for mode, count in sorted(breakdown.items()):
            print(f"  {mode:<34} {count}")

    if args.write_baseline:
        write_baseline(current, args.baseline)
        print(f"\nBaseline written to {args.baseline}")
        return 0

    if not args.baseline.exists():
        print(
            f"\nNo baseline at {args.baseline}; nothing to compare against. "
            "Create one with --write-baseline.",
            file=sys.stderr,
        )
        return 0

    report = compare_with_baseline(current, load_baseline(args.baseline))
    result = EvaluationResult(run=current, scores=scores, report=report)

    print("\n" + report.format_report())

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nReport written to {args.json_out}")

    # Non-zero fails the CI job.
    return 1 if result.has_regression else 0


def main() -> int:  # pragma: no cover - thin entry point
    return run()
