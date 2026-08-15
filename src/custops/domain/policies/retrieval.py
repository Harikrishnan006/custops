"""When is retrieved evidence good enough to reason from?

BUILD_SPEC §7 routes the graph on this: sufficient evidence goes to the decision
node, low retrieval confidence escalates to a human. That branch must be a
deterministic function of the retrieval scores — if a model decided whether its
own evidence was sufficient, "I don't have enough information" would be
precisely the judgement least likely to survive contact with an eager model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """Thresholds for accepting a retrieval result.

    ``minimum_similarity`` is cosine similarity in [-1, 1]; for normalised
    embeddings, practically [0, 1]. 0.35 is a deliberately permissive floor —
    its job is to catch *nothing relevant was found*, not to rank.
    """

    minimum_similarity: float = 0.35
    minimum_results: int = 1
    # Above this, the top hit is strong enough that a thin result set is still
    # trustworthy — one exact policy match beats five vague ones.
    strong_match_similarity: float = 0.75


@dataclass(frozen=True, slots=True)
class SufficiencyAssessment:
    """Whether evidence supports a decision, and how confident that is."""

    sufficient: bool
    confidence: float
    reason: str


def assess_sufficiency(
    similarities: list[float],
    policy: RetrievalPolicy | None = None,
) -> SufficiencyAssessment:
    """Judge a retrieval result from its similarity scores alone.

    Deliberately ignores the *content* of the matches: content-based judgement
    is the model's job, and letting it feed back into the sufficiency gate would
    let a confident-sounding irrelevant chunk unlock a decision it should have
    escalated.
    """
    active = policy if policy is not None else RetrievalPolicy()

    if not similarities:
        return SufficiencyAssessment(
            sufficient=False,
            confidence=0.0,
            reason="No matching evidence was retrieved.",
        )

    ranked = sorted(similarities, reverse=True)
    top = ranked[0]
    usable = [score for score in ranked if score >= active.minimum_similarity]

    if top < active.minimum_similarity:
        return SufficiencyAssessment(
            sufficient=False,
            confidence=round(max(top, 0.0), 4),
            reason=(
                f"Best match scored {top:.3f}, below the {active.minimum_similarity:.2f} minimum."
            ),
        )

    if top >= active.strong_match_similarity:
        return SufficiencyAssessment(
            sufficient=True,
            confidence=round(top, 4),
            reason=f"Strong match ({top:.3f}) above the strong-match threshold.",
        )

    if len(usable) < active.minimum_results:
        return SufficiencyAssessment(
            sufficient=False,
            confidence=round(top, 4),
            reason=(
                f"Only {len(usable)} result(s) cleared the similarity floor; "
                f"{active.minimum_results} required."
            ),
        )

    return SufficiencyAssessment(
        sufficient=True,
        confidence=round(top, 4),
        reason=f"{len(usable)} result(s) cleared the similarity floor; best {top:.3f}.",
    )
