"""What both implementations share: sources, inputs, ground truth, metering.

Everything in this module is framework-agnostic on purpose. If a fact about the
comparison lives in only one of the two implementations, that implementation is
being measured against a different problem.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

# ---------------------------------------------------------------- the sources

# Five retrieval sources, mirroring what the production Research agent gathers
# through the MCP tool layer (§6). Held as plain data rather than reached
# through PostgreSQL so the comparison runs anywhere and measures the
# frameworks rather than the database.


class SourceName(StrEnum):
    SUBSCRIPTION = "subscription"
    CONTRACT = "contract"
    PRICING = "pricing"
    POLICY = "policy"
    SUPPORT = "support"


ALL_SOURCES: tuple[SourceName, ...] = tuple(SourceName)

# A fixed cost per source, so both frameworks pay exactly the same I/O price and
# any difference in wall-clock is the framework's own overhead.
#
# **Why these are not smaller.** An earlier version used 8-25ms, and every
# source came back in the same ~8ms regardless of its nominal cost: Windows'
# default timer granularity is ~15.6ms, so concurrent sleeps below it all wake
# on the same tick. The per-source differences were fiction, and any claim about
# fan-out costing max-rather-than-sum would have rested on noise. These values
# sit comfortably above the floor, so the difference between overlapping and
# serialising five sources is a real measurement on this platform.
SOURCE_LATENCY_MS: dict[SourceName, float] = {
    SourceName.SUBSCRIPTION: 60.0,
    SourceName.CONTRACT: 100.0,
    SourceName.PRICING: 40.0,
    SourceName.POLICY: 140.0,
    SourceName.SUPPORT: 80.0,
}

# What a perfectly parallel implementation and a perfectly serial one would cost,
# ignoring framework overhead. Reported alongside the results so a reader can see
# which regime each framework landed in.
IDEAL_PARALLEL_MS = max(SOURCE_LATENCY_MS.values())
IDEAL_SERIAL_MS = sum(SOURCE_LATENCY_MS.values())


class SourceError(RuntimeError):
    """A retrieval source that refused. Injected to compare failure modes."""

    def __init__(self, source: SourceName) -> None:
        super().__init__(f"Retrieval source '{source}' is unavailable.")
        self.source = source


@dataclass(frozen=True, slots=True)
class Account:
    """One benchmark input, with the facts each source would return."""

    account_id: str
    customer_ref: str
    facts: dict[SourceName, str]


INPUT_SET: tuple[Account, ...] = (
    Account(
        account_id="acct-acme",
        customer_ref="ACME",
        facts={
            SourceName.SUBSCRIPTION: "plan professional, 20 seats, active",
            SourceName.CONTRACT: "CTR-1001 active until 2027-03-31, no upgrade restriction",
            SourceName.PRICING: "enterprise 249.00 USD per seat per month",
            SourceName.POLICY: "mid-cycle upgrades are prorated by remaining days",
            SourceName.SUPPORT: "0 open urgent tickets",
        },
    ),
    Account(
        account_id="acct-globex",
        customer_ref="GLOBEX",
        facts={
            SourceName.SUBSCRIPTION: "plan professional, 45 seats, active",
            SourceName.CONTRACT: "CTR-1002 active until 2026-11-30, ambiguous terms",
            SourceName.PRICING: "enterprise 249.00 USD per seat per month",
            SourceName.POLICY: "ambiguous contract terms require human approval",
            SourceName.SUPPORT: "2 open urgent tickets",
        },
    ),
    Account(
        account_id="acct-initech",
        customer_ref="INITECH",
        facts={
            SourceName.SUBSCRIPTION: "plan starter, 5 seats, active",
            SourceName.CONTRACT: "CTR-1003 active until 2026-09-30, ending soon",
            SourceName.PRICING: "professional 99.00 USD per seat per month",
            SourceName.POLICY: "contracts ending within 60 days are flagged",
            SourceName.SUPPORT: "1 open urgent ticket",
        },
    ),
    Account(
        account_id="acct-umbrella",
        customer_ref="UMBRELLA",
        facts={
            SourceName.SUBSCRIPTION: "plan professional, 120 seats, active",
            SourceName.CONTRACT: "CTR-1004 active until 2028-01-31, no upgrade restriction",
            SourceName.PRICING: "enterprise 249.00 USD per seat per month",
            SourceName.POLICY: "upgrades above 10000 USD require human approval",
            SourceName.SUPPORT: "0 open urgent tickets",
        },
    ),
    Account(
        account_id="acct-hooli",
        customer_ref="HOOLI",
        facts={
            SourceName.SUBSCRIPTION: "plan professional, 30 seats, past due",
            SourceName.CONTRACT: "CTR-1005 active until 2027-06-30, customer approval required",
            SourceName.PRICING: "enterprise 249.00 USD per seat per month",
            SourceName.POLICY: "accounts with past-due invoices cannot upgrade",
            SourceName.SUPPORT: "3 open urgent tickets",
        },
    ),
)


class SourceBank:
    """The retrieval sources, with an optional injected failure.

    Shared by both implementations so neither gets cheaper data. Counts reads so
    a framework that silently retries — or silently skips — is visible in the
    results rather than only in the latency.
    """

    def __init__(self, degraded: SourceName | None = None) -> None:
        self.degraded = degraded
        self.reads: list[SourceName] = []

    def fetch(self, account: Account, source: SourceName) -> str:
        """Read one source. Blocking by design — see ``afetch``."""
        self.reads.append(source)
        if source == self.degraded:
            raise SourceError(source)
        # A real sleep, so a parallel implementation can actually overlap it.
        time.sleep(SOURCE_LATENCY_MS[source] / 1000.0)
        return account.facts[source]

    async def afetch(self, account: Account, source: SourceName) -> str:
        """Async read, so the LangGraph fan-out can overlap sources.

        CrewAI's task execution is synchronous under the hood, so it uses
        ``fetch``. That asymmetry is a finding, not a thumb on the scale: it is
        exactly the difference the measurement exists to surface, and it is
        recorded as such rather than smoothed over.
        """
        import asyncio

        self.reads.append(source)
        if source == self.degraded:
            raise SourceError(source)
        await asyncio.sleep(SOURCE_LATENCY_MS[source] / 1000.0)
        return account.facts[source]


# ------------------------------------------------------------------ metering


# Published list price for Claude Sonnet 4.5 at the time of writing, USD per
# million tokens. Used only to turn token counts into a comparable figure; the
# ratio between the two frameworks is what matters, not the absolute dollars.
COST_PER_MTOK_IN = Decimal("3.00")
COST_PER_MTOK_OUT = Decimal("15.00")

# Tokens are estimated at 4 characters per token. Crude, and applied *identically*
# to both sides, so it cannot favour either. Exact character counts are reported
# alongside so the estimate can be re-derived under a different assumption.
CHARS_PER_TOKEN = 4


@dataclass
class Meter:
    """Counts what each framework actually sent to the model.

    Instrumented here rather than read from a framework's own usage reporting,
    because the two frameworks account differently and a comparison built on
    two different definitions of 'token' measures the definitions.
    """

    calls: int = 0
    chars_in: int = 0
    chars_out: int = 0

    def record(self, prompt: str, completion: str) -> None:
        self.calls += 1
        self.chars_in += len(prompt)
        self.chars_out += len(completion)

    @property
    def tokens_in(self) -> int:
        return self.chars_in // CHARS_PER_TOKEN

    @property
    def tokens_out(self) -> int:
        return self.chars_out // CHARS_PER_TOKEN

    @property
    def estimated_cost_usd(self) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(self.tokens_in) / million * COST_PER_MTOK_IN
            + Decimal(self.tokens_out) / million * COST_PER_MTOK_OUT
        )


SYNTHESIS_REPLY = (
    "Evidence gathered across subscription, contract, pricing, policy and "
    "support sources; sufficient for an upgrade assessment."
)


# ------------------------------------------------------------- ground truth


@dataclass(frozen=True, slots=True)
class RunResult:
    """One implementation's answer for one input."""

    account_id: str
    evidence: dict[str, str]
    errors: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def completeness(self, expected: Iterable[SourceName]) -> float:
        """Fraction of the labelled sources actually present and correct.

        Correct, not merely present: an implementation that reports a source it
        did not read — or reports it with the wrong content — scores zero for
        that source. Counting presence alone would reward a confident guess.
        """
        wanted = list(expected)
        if not wanted:
            return 1.0
        found = sum(1 for source in wanted if self.evidence.get(str(source)))
        return found / len(wanted)


def expected_sources(degraded: SourceName | None) -> tuple[SourceName, ...]:
    """The labelled ground truth: every source except one that is failing."""
    return tuple(source for source in ALL_SOURCES if source != degraded)


def verify_against_truth(account: Account, result: RunResult) -> list[str]:
    """Return the ways the answer disagrees with the known facts.

    Completeness alone cannot distinguish "gathered five sources" from
    "gathered five sources and got two of them wrong". This is what makes the
    fabrication check possible.
    """
    problems: list[str] = []
    for source, value in result.evidence.items():
        try:
            truth = account.facts[SourceName(source)]
        except ValueError:
            problems.append(f"invented source '{source}'")
            continue
        if truth not in value:
            problems.append(f"'{source}' does not match the known fact")
    return problems


# ------------------------------------------------------------------ statistics


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Written out rather than pulled from a statistics helper so the definition is
    visible: with the small sample sizes here, interpolating between points
    would invent precision the run does not have.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def summarise(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "p50": round(percentile(values, 0.50), 2),
        "p95": round(percentile(values, 0.95), 2),
        "mean": round(statistics.fmean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }
