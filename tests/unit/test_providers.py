"""Provider abstraction — capability boundaries and the test double."""

from __future__ import annotations

import math
from typing import ClassVar

import pytest

from custops.config import ProviderSettings, Settings
from custops.providers.anthropic_provider import AnthropicEmbeddingProvider
from custops.providers.base import (
    Capability,
    CapabilityNotSupportedError,
    EmbeddingProvider,
    ProviderError,
    ProviderName,
    ProviderNotConfiguredError,
)
from custops.providers.deterministic import DeterministicEmbeddingProvider
from custops.providers.google_provider import GoogleEmbeddingProvider
from custops.providers.openai_provider import OpenAIEmbeddingProvider
from custops.providers.registry import describe_capabilities, get_embedding_provider


def _settings(**provider_overrides: object) -> Settings:
    defaults: dict[str, object] = {"embedding_provider": ProviderName.DETERMINISTIC}
    defaults.update(provider_overrides)
    environment = defaults.pop("environment", "test")
    return Settings(
        _env_file=None,
        environment=environment,
        providers=ProviderSettings(_env_file=None, **defaults),  # type: ignore[arg-type]
    )


class TestDeterministicProvider:
    async def test_same_text_always_yields_the_same_vector(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=64)

        first = await provider.embed(["upgrade eligibility"])
        second = await provider.embed(["upgrade eligibility"])

        assert first.vectors == second.vectors

    async def test_different_text_yields_different_vectors(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=64)

        result = await provider.embed(["refund policy", "upgrade policy"])

        assert result.vectors[0] != result.vectors[1]

    async def test_vectors_are_unit_length(self) -> None:
        """Cosine distance is only meaningful for normalised vectors."""
        provider = DeterministicEmbeddingProvider(dimensions=128)

        result = await provider.embed(["anything"])

        magnitude = math.sqrt(sum(value * value for value in result.vectors[0]))
        assert magnitude == pytest.approx(1.0)

    async def test_dimensionality_is_honoured(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=1536)

        result = await provider.embed(["x"])

        assert len(result.vectors[0]) == 1536
        assert result.dimensions == 1536

    async def test_order_is_preserved(self) -> None:
        """vector[i] must correspond to texts[i] or every chunk is mislabelled."""
        provider = DeterministicEmbeddingProvider(dimensions=32)
        texts = ["alpha", "bravo", "charlie"]

        batch = await provider.embed(texts)
        individually = [(await provider.embed([text])).vectors[0] for text in texts]

        assert list(batch.vectors) == individually

    async def test_empty_batch_returns_no_vectors(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=32)

        assert (await provider.embed([])).vectors == ()

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(DeterministicEmbeddingProvider(dimensions=8), EmbeddingProvider)

    def test_invalid_dimensions_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimensions must be positive"):
            DeterministicEmbeddingProvider(dimensions=0)


class TestAnthropicCapabilityBoundary:
    """Anthropic has no embeddings endpoint — this must fail loudly, not fake it."""

    async def test_embed_raises_capability_not_supported(self) -> None:
        with pytest.raises(CapabilityNotSupportedError) as error:
            await AnthropicEmbeddingProvider().embed(["anything"])

        assert error.value.provider == ProviderName.ANTHROPIC
        assert error.value.capability == Capability.EMBEDDING

    def test_model_and_dimensions_also_raise(self) -> None:
        provider = AnthropicEmbeddingProvider()

        with pytest.raises(CapabilityNotSupportedError):
            _ = provider.model
        with pytest.raises(CapabilityNotSupportedError):
            _ = provider.dimensions

    def test_capability_matrix_records_chat_only(self) -> None:
        capabilities = describe_capabilities()

        assert capabilities[ProviderName.ANTHROPIC] == (Capability.CHAT,)
        assert Capability.EMBEDDING in capabilities[ProviderName.OPENAI]
        assert Capability.EMBEDDING in capabilities[ProviderName.GOOGLE]


class TestRegistry:
    def test_deterministic_is_allowed_in_test(self) -> None:
        provider = get_embedding_provider(_settings(environment="test"))

        assert isinstance(provider, DeterministicEmbeddingProvider)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_deterministic_is_refused_outside_local_and_test(self, environment: str) -> None:
        """Meaningless similarity scores in a deployed environment are worse
        than no retrieval at all."""
        with pytest.raises(ProviderError, match="meaningless similarity"):
            get_embedding_provider(_settings(environment=environment))

    def test_anthropic_for_embeddings_is_rejected_at_configuration_time(self) -> None:
        with pytest.raises(CapabilityNotSupportedError):
            get_embedding_provider(_settings(embedding_provider=ProviderName.ANTHROPIC))

    def test_unknown_provider_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="Unknown embedding provider"):
            get_embedding_provider(_settings(embedding_provider="acme-embeddings"))

    def test_openai_without_a_key_fails_before_any_call(self) -> None:
        with pytest.raises(ProviderNotConfiguredError) as error:
            get_embedding_provider(
                _settings(embedding_provider=ProviderName.OPENAI, environment="test")
            )

        assert error.value.missing == "OPENAI_API_KEY"

    def test_google_without_a_key_fails_before_any_call(self) -> None:
        with pytest.raises(ProviderNotConfiguredError):
            GoogleEmbeddingProvider(api_key="")

    def test_dimensions_come_from_configuration(self) -> None:
        provider = get_embedding_provider(_settings(embedding_dimensions=256))

        assert provider.dimensions == 256


class TestOpenAIAdapterOrdering:
    async def test_vectors_are_reordered_by_index(self) -> None:
        """A mis-ordered batch would attach every chunk to the wrong text."""

        class _Item:
            def __init__(self, index: int, embedding: list[float]) -> None:
                self.index = index
                self.embedding = embedding

        class _Response:
            # Deliberately out of order — the adapter must sort by index.
            data: ClassVar[list[_Item]] = [
                _Item(2, [0.3]),
                _Item(0, [0.1]),
                _Item(1, [0.2]),
            ]

        class _Embeddings:
            async def create(self, **_: object) -> _Response:
                return _Response()

        class _Client:
            embeddings = _Embeddings()

        provider = OpenAIEmbeddingProvider(api_key="unused", client=_Client(), dimensions=1)

        result = await provider.embed(["a", "b", "c"])

        assert result.vectors == ((0.1,), (0.2,), (0.3,))
        assert result.provider == ProviderName.OPENAI
