"""The retrieval gate, in both directions, under both embedders.

A similarity threshold is a property of the embedding model, not of the business
rule. Production runs a real embedding model and uses 0.35; the integration
suite runs a deterministic lexical double whose scores sit on a different scale
and uses a calibrated 0.10.

The risk in that arrangement is obvious: a "calibrated" threshold is one edit
away from being a bypass. So these tests prove all four corners —

* genuinely relevant evidence clears the calibrated threshold;
* genuinely unrelated evidence does **not**, and is reported insufficient;
* production still defaults to 0.35;
* the override cannot leak into production.

They need no database: the embedder is pure, and ``assess_sufficiency`` takes
similarity scores rather than rows.

One thing these tests are careful about, because getting it wrong once already
cost a CI round: the population measured here is the population the pipeline
embeds. Ingestion chunks each policy *body* and embeds the chunks individually,
so a stored vector represents neither the title nor the whole document.
Measuring ``title + body`` instead roughly doubles the scores and calibrates the
threshold against evidence that never reaches the gate. Everything below runs
through the real ``chunk_text``.
"""

from __future__ import annotations

import pytest

from custops.domain.policies.retrieval import RetrievalPolicy, assess_sufficiency
from custops.domain.seed import POLICIES
from custops.knowledge.ingestion.chunking import chunk_text
from custops.providers.deterministic import DeterministicEmbeddingProvider
from tests.integration.conftest import TEST_RETRIEVAL_MINIMUM_SIMILARITY

PROVIDER = DeterministicEmbeddingProvider(dimensions=1536)

# The query shape the research node actually issues (see agents/nodes.py).
RELEVANT_QUERY = "eligibility and contract terms for upgrading to enterprise"

UNRELATED_TEXTS = (
    "the quick brown fox jumps over a lazy dog",
    "weather forecast for tuesday afternoon rain showers",
    "recipe for sourdough bread with rye flour and salt",
    "quarterly shipping logistics for warehouse pallets",
)


def _chunks(*texts: str) -> tuple[str, ...]:
    """Exactly what ingestion stores: body chunks, one vector each."""
    return tuple(chunk.text for text in texts for chunk in chunk_text(text))


CORPUS = _chunks(*(policy["body"] for policy in POLICIES))
UNRELATED_CORPUS = _chunks(*UNRELATED_TEXTS)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


async def _similarities(query: str, documents: tuple[str, ...]) -> list[float]:
    vectors = (await PROVIDER.embed([query, *documents])).vectors
    return [_cosine(vectors[0], vector) for vector in vectors[1:]]


# ------------------------------------------------- the double carries signal


async def test_relevant_content_scores_above_the_calibrated_threshold() -> None:
    """Without this the integration suite would escalate every workflow."""
    scores = await _similarities(RELEVANT_QUERY, CORPUS)

    assert max(scores) >= TEST_RETRIEVAL_MINIMUM_SIMILARITY


async def test_unrelated_content_scores_below_the_calibrated_threshold() -> None:
    """The half that stops calibration becoming a bypass.

    If this ever fails, the threshold is admitting noise and the gate has
    stopped meaning anything.
    """
    scores = await _similarities(RELEVANT_QUERY, UNRELATED_CORPUS)

    assert max(scores) < TEST_RETRIEVAL_MINIMUM_SIMILARITY


async def test_the_threshold_sits_between_the_two_populations_with_margin() -> None:
    """Pins the measurement the threshold was derived from.

    A content edit that erodes the separation should fail here — where the
    reason is obvious — rather than as a puzzling escalation in CI.
    """
    relevant = max(await _similarities(RELEVANT_QUERY, CORPUS))
    unrelated = max(await _similarities(RELEVANT_QUERY, UNRELATED_CORPUS))

    assert unrelated < TEST_RETRIEVAL_MINIMUM_SIMILARITY < relevant
    # Meaningful daylight on both sides, not a hairline pass. Expressed relative
    # to the threshold: these scores sit on the double's scale rather than
    # production's, so an absolute margin would be a number with no meaning.
    assert relevant - TEST_RETRIEVAL_MINIMUM_SIMILARITY > 0.4 * TEST_RETRIEVAL_MINIMUM_SIMILARITY
    assert TEST_RETRIEVAL_MINIMUM_SIMILARITY - unrelated > 0.5 * TEST_RETRIEVAL_MINIMUM_SIMILARITY


async def test_every_genuine_match_clears_the_threshold_not_merely_the_best() -> None:
    """The gate reads the top score, but a corpus where only one chunk clears is
    a corpus one edit away from clearing none."""
    scoring = [score for score in await _similarities(RELEVANT_QUERY, CORPUS) if score > 0.0]

    assert scoring, "no seeded chunk shares any term with the research query"
    assert min(scoring) > TEST_RETRIEVAL_MINIMUM_SIMILARITY


async def test_chunk_scores_are_what_the_gate_sees_not_whole_document_scores() -> None:
    """Guards the trap this calibration originally fell into.

    Embedding `title + body` scores materially higher than embedding the body
    chunks ingestion actually stores. Calibrating on the former yields a
    threshold the real pipeline never clears — which is exactly what happened,
    and cost a full CI round to diagnose. If the two ever converge this test can
    go; while they differ, the distinction must stay visible.
    """
    whole = tuple(f"{policy['title']} {policy['body']}" for policy in POLICIES)

    assert max(await _similarities(RELEVANT_QUERY, whole)) > max(
        await _similarities(RELEVANT_QUERY, CORPUS)
    )


# --------------------------------------------- the gate still escalates


async def test_unrelated_evidence_is_reported_insufficient() -> None:
    """The escalation path stays exercised under the calibrated threshold.

    This is what a blanket-permissive policy would destroy: with one, nothing
    would ever be insufficient and the low-confidence branch would be dead code.
    """
    scores = await _similarities(RELEVANT_QUERY, UNRELATED_CORPUS)

    verdict = assess_sufficiency(
        scores, policy=RetrievalPolicy(minimum_similarity=TEST_RETRIEVAL_MINIMUM_SIMILARITY)
    )

    assert not verdict.sufficient
    assert "below" in verdict.reason.lower()


async def test_relevant_evidence_is_reported_sufficient() -> None:
    scores = await _similarities(RELEVANT_QUERY, CORPUS)

    verdict = assess_sufficiency(
        scores, policy=RetrievalPolicy(minimum_similarity=TEST_RETRIEVAL_MINIMUM_SIMILARITY)
    )

    assert verdict.sufficient


async def test_an_empty_result_is_insufficient_under_any_threshold() -> None:
    for threshold in (TEST_RETRIEVAL_MINIMUM_SIMILARITY, RetrievalPolicy().minimum_similarity):
        verdict = assess_sufficiency([], policy=RetrievalPolicy(minimum_similarity=threshold))

        assert not verdict.sufficient


# ------------------------------------------------------ production is intact


def test_production_default_is_unchanged() -> None:
    """The number this whole exercise was forbidden to move."""
    assert RetrievalPolicy().minimum_similarity == 0.35


def test_the_calibrated_threshold_is_lower_than_production_but_not_zero() -> None:
    """Calibrated, not disabled."""
    assert 0.0 < TEST_RETRIEVAL_MINIMUM_SIMILARITY < RetrievalPolicy().minimum_similarity


def test_the_production_dependency_returns_no_override() -> None:
    """`None` means "use the default" — the override exists only where a test
    installs it, so nothing can leak into a deployed process."""
    from custops.apps.api.routers.workflows import get_retrieval_policy

    assert get_retrieval_policy() is None


def test_the_runner_defaults_to_the_production_policy() -> None:
    """A runner built without an explicit policy must not pick up a test one."""
    import inspect

    from custops.apps.orchestrator.runner import WorkflowRunner

    parameter = inspect.signature(WorkflowRunner.__init__).parameters["retrieval_policy"]

    assert parameter.default is None


def test_node_dependencies_default_to_the_production_policy() -> None:
    import inspect

    from custops.agents.nodes import NodeDependencies

    field = inspect.signature(NodeDependencies).parameters["retrieval_policy"]

    assert field.default is None


@pytest.mark.parametrize("threshold", [0.35, 0.10])
def test_the_gate_shape_is_identical_at_either_threshold(threshold: float) -> None:
    """Only *where* the line sits changes, never whether one exists.

    Scores below the threshold are insufficient and scores above it are
    sufficient, at both calibrations.
    """
    policy = RetrievalPolicy(minimum_similarity=threshold)

    assert not assess_sufficiency([threshold - 0.01], policy=policy).sufficient
    assert assess_sufficiency([threshold + 0.4], policy=policy).sufficient
