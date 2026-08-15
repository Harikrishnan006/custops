"""OpenAI embedding adapter.

The SDK is imported lazily inside the constructor so that importing this module
never requires the dependency to be installed, and so a missing package
produces an actionable message rather than an ImportError from three frames
deep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custops.providers.base import (
    EmbeddingResult,
    ProviderError,
    ProviderName,
    ProviderNotConfiguredError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

# text-embedding-3-small: 1536 dimensions, the cost/quality default.
# text-embedding-3-large is 3072 — changing model changes the stored column
# width, which is a migration, not a config tweak. See ADR-005.
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 1536


class OpenAIEmbeddingProvider:
    """Embeddings via OpenAI's embeddings endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise ProviderNotConfiguredError(ProviderName.OPENAI, "OPENAI_API_KEY")

        self._model = model
        self._dimensions = dimensions

        if client is not None:
            self._client = client
        else:
            try:
                from openai import AsyncOpenAI
            except ImportError as error:  # pragma: no cover - dependency guard
                raise ProviderError(
                    "The 'openai' package is required for the OpenAI provider. "
                    "Install it with: uv add openai"
                ) from error
            self._client = AsyncOpenAI(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(
                vectors=(),
                model=self._model,
                dimensions=self._dimensions,
                provider=ProviderName.OPENAI,
            )

        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions,
        )
        # Sort by index rather than trusting response order: the contract is
        # that vector[i] corresponds to texts[i], and a mis-ordered batch would
        # attach every chunk to the wrong text with no visible error.
        ordered = sorted(response.data, key=lambda item: item.index)
        return EmbeddingResult(
            vectors=tuple(tuple(item.embedding) for item in ordered),
            model=self._model,
            dimensions=self._dimensions,
            provider=ProviderName.OPENAI,
        )
