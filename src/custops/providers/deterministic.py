"""A deterministic embedding stand-in for tests and offline development.

**This is a test double, not a feature.** It does not approximate semantic
similarity and must never be selected in a deployed environment — the registry
refuses it outside `local` and `test` (Rule 6: no fake implementations dressed
up as real ones).

What it *is* good for is everything about the retrieval pipeline that has
nothing to do with embedding quality: chunking, storage, index usage, ordering,
Evidence assembly, and the SQL itself can all be exercised end-to-end without an
API key, without network access, and without spending money — and the results
are byte-identical on every run, so a retrieval test that fails is a real
regression rather than model drift.

**Lexical, not semantic.** A text is represented by the set of terms it
contains, hashed into dimensions. Texts sharing vocabulary are similar; texts
that do not are orthogonal. It will not recognise a paraphrase sharing no words,
which a real embedding model would — so it is a stand-in for the *plumbing*, not
for the model.

The scores it produces are on a different scale from a real embedding model's,
and that is a property of the method rather than a defect: cosine between
L2-normalised term-presence vectors is ``shared / sqrt(|a| * |b|)``, which for a
short question against a long document lands well below what a trained model
returns for the same pair. Callers that gate on a similarity threshold must use
one calibrated to whichever provider is in use — see
``RetrievalPolicy`` and ``tests/integration/conftest.py``.

An earlier version hashed the whole string into one pseudo-random vector. It met
every contract below, but similarity carried no signal at all: unrelated text
scored *higher* than genuinely relevant policies. Nineteen integration tests
failed on it the first time they ran against a real database, and the failure
read as a product bug rather than a limitation of the double.
"""

from __future__ import annotations

import hashlib
import math
import re

from custops.providers.base import EmbeddingResult, ProviderName

# Bumped: v1 hashed whole strings and carried no lexical signal. Stored vectors
# from v1 are not comparable with these.
MODEL_NAME = "deterministic-lexical-v2"

_TOKEN = re.compile(r"[a-z0-9]+")

# Terms too common to carry topical signal. Deliberately short — a long list
# would start encoding judgements about language into a test double.
_STOPWORDS = frozenset(
    (
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "is",
    "it", "its", "of", "on", "or", "that", "the", "to", "was", "were", "will", "with",
    "without", "which", "than", "then", "this", "these", "those", "must", "may", "can", "not",
    "no", "any", "all", "if", "when", "where", "who", "whom", "whose", "into", "over", "under",
    "about"
    )
)

# Two-character fragments collide constantly and add noise rather than signal.
_MIN_TOKEN_LENGTH = 3


class DeterministicEmbeddingProvider:
    """Hash text into a stable unit vector.

    Derived from a SHA-256 digest of the text, expanded to the required width.
    Identical text always produces an identical vector, and different text
    almost always produces a very different one — which is enough to assert
    "the right chunk came back" without asserting anything about meaning.
    """

    def __init__(self, dimensions: int = 1536) -> None:
        if dimensions < 1:
            raise ValueError(f"dimensions must be positive, got {dimensions}")
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return MODEL_NAME

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors = tuple(self._vector(text) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            model=MODEL_NAME,
            dimensions=self._dimensions,
            provider=ProviderName.DETERMINISTIC,
        )

    def _vector(self, text: str) -> tuple[float, ...]:
        """Hash the text's distinct terms into a unit vector.

        Presence, not frequency: whether a term occurs is the signal, and how
        often it repeats is not. Frequency weighting would make a document's
        norm depend on its own verbosity, which changes similarity without
        changing relevance.
        """
        terms = self._terms(text)
        if not terms:
            # Nothing lexical to work with — fall back to hashing the whole
            # string so distinct inputs still yield distinct vectors.
            return self._digest_vector(text)

        raw = [0.0] * self._dimensions
        for term in terms:
            digest = hashlib.sha256(term.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            # An independent sign bit, so two terms sharing a dimension are as
            # likely to cancel as to reinforce. Without it, collisions could
            # only ever inflate similarity.
            raw[index] += 1.0 if digest[4] & 1 else -1.0

        return self._normalise(raw, text)

    def _terms(self, text: str) -> frozenset[str]:
        """Distinct, stemmed, non-trivial terms."""
        return frozenset(
            stemmed
            for match in _TOKEN.finditer(text.lower())
            if len(stemmed := self._stem(match.group())) >= _MIN_TOKEN_LENGTH
            and stemmed not in _STOPWORDS
        )

    @staticmethod
    def _stem(token: str) -> str:
        """Crude suffix stripping, so an inflection does not split a term.

        Not linguistics: just enough that "upgrade", "upgrades" and "upgrading"
        reduce to one term. Without the trailing-'e' step they reduce to three,
        and a question about upgrading matches a policy about upgrades only by
        accident.
        """
        for suffix in ("ing", "ed", "es", "s"):
            if len(token) - len(suffix) >= 4 and token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        if len(token) >= 5 and token.endswith("e"):
            token = token[:-1]
        return token

    def _digest_vector(self, text: str) -> tuple[float, ...]:
        raw: list[float] = []
        counter = 0
        while len(raw) < self._dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            raw.extend(byte / 255.0 - 0.5 for byte in digest)
            counter += 1
        return self._normalise(raw[: self._dimensions], "")

    def _normalise(self, raw: list[float], text: str) -> tuple[float, ...]:
        """Unit length, so cosine distance behaves sensibly."""
        magnitude = math.sqrt(sum(value * value for value in raw))
        if magnitude == 0:  # pragma: no cover - only for a pathological input
            return self._digest_vector(text) if text else tuple(0.0 for _ in raw)
        return tuple(value / magnitude for value in raw)
