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
"""

from __future__ import annotations

import hashlib
import math

from custops.providers.base import EmbeddingResult, ProviderName

MODEL_NAME = "deterministic-hash-v1"


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
        raw: list[float] = []
        counter = 0
        # Expand the digest until it covers the required dimensionality.
        while len(raw) < self._dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            raw.extend(byte / 255.0 - 0.5 for byte in digest)
            counter += 1

        trimmed = raw[: self._dimensions]
        # Normalise to unit length so cosine distance behaves sensibly.
        magnitude = math.sqrt(sum(value * value for value in trimmed))
        if magnitude == 0:  # pragma: no cover - only for a pathological digest
            return tuple(0.0 for _ in trimmed)
        return tuple(value / magnitude for value in trimmed)
