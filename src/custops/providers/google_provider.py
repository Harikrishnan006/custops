"""Google embedding adapter."""

from __future__ import annotations

import importlib
from typing import Any

from custops.providers.base import (
    EmbeddingResult,
    ProviderError,
    ProviderName,
    ProviderNotConfiguredError,
)

DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIMENSIONS = 1536


class GoogleEmbeddingProvider:
    """Embeddings via Google's generative-AI embedding endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise ProviderNotConfiguredError(ProviderName.GOOGLE, "GOOGLE_API_KEY")

        self._model = model
        self._dimensions = dimensions

        if client is not None:
            self._client = client
        else:
            # Imported dynamically because 'google-genai' is optional and not a
            # declared dependency. A static `from google import genai` also fails
            # type checking whenever some *other* distribution owns the shared
            # `google` namespace — a2a-sdk's protobuf dependencies do exactly
            # that — which turns an absent optional package into a type error in
            # an unrelated build.
            try:
                genai = importlib.import_module("google.genai")
            except ImportError as error:  # pragma: no cover - dependency guard
                raise ProviderError(
                    "The 'google-genai' package is required for the Google provider. "
                    "Install it with: uv add google-genai"
                ) from error
            self._client = genai.Client(api_key=api_key)

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
                provider=ProviderName.GOOGLE,
            )

        response = await self._client.aio.models.embed_content(
            model=self._model,
            contents=texts,
            config={"output_dimensionality": self._dimensions},
        )
        return EmbeddingResult(
            vectors=tuple(tuple(item.values) for item in response.embeddings),
            model=self._model,
            dimensions=self._dimensions,
            provider=ProviderName.GOOGLE,
        )
