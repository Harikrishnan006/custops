"""Provider selection from configuration.

The D11 test: adding a provider is a new adapter plus a branch here. No caller
of ``get_embedding_provider`` changes, and no business logic mentions a vendor.
"""

from __future__ import annotations

from custops.config import Settings
from custops.providers.base import (
    Capability,
    CapabilityNotSupportedError,
    EmbeddingProvider,
    ProviderError,
    ProviderName,
)
from custops.providers.deterministic import DeterministicEmbeddingProvider
from custops.providers.google_provider import GoogleEmbeddingProvider
from custops.providers.openai_provider import OpenAIEmbeddingProvider

# Environments where the deterministic stand-in may be used. Anywhere else it
# would mean shipping meaningless similarity scores to real users.
STAND_IN_ALLOWED_ENVIRONMENTS = frozenset({"local", "test"})


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Build the configured embedding provider.

    Fails at construction rather than on first use: a workflow that discovers
    its embedder is unusable halfway through retrieval has already burned a
    turn and half an execution trace.
    """
    name = settings.providers.embedding_provider
    dimensions = settings.providers.embedding_dimensions

    if name == ProviderName.DETERMINISTIC:
        if settings.environment not in STAND_IN_ALLOWED_ENVIRONMENTS:
            raise ProviderError(
                "The deterministic embedding stand-in produces meaningless "
                f"similarity scores and is not permitted in environment "
                f"'{settings.environment}'. Configure a real provider."
            )
        return DeterministicEmbeddingProvider(dimensions=dimensions)

    if name == ProviderName.OPENAI:
        return OpenAIEmbeddingProvider(
            api_key=settings.providers.openai_api_key.get_secret_value(),
            model=settings.providers.embedding_model,
            dimensions=dimensions,
        )

    if name == ProviderName.GOOGLE:
        return GoogleEmbeddingProvider(
            api_key=settings.providers.google_api_key.get_secret_value(),
            model=settings.providers.embedding_model,
            dimensions=dimensions,
        )

    if name == ProviderName.ANTHROPIC:
        # Declared explicitly rather than falling through to a generic error:
        # "Anthropic has no embeddings API" is a fact worth stating once, in
        # the place someone will look when the config is rejected.
        raise CapabilityNotSupportedError(ProviderName.ANTHROPIC, Capability.EMBEDDING)

    raise ProviderError(f"Unknown embedding provider: '{name}'.")


def describe_capabilities() -> dict[str, tuple[str, ...]]:
    """What each provider actually implements.

    Used by diagnostics and documentation so the capability matrix has one
    source of truth rather than being restated in prose that drifts.
    """
    return {
        ProviderName.OPENAI: (Capability.CHAT, Capability.EMBEDDING),
        ProviderName.GOOGLE: (Capability.CHAT, Capability.EMBEDDING),
        # Chat only — no embeddings endpoint exists.
        ProviderName.ANTHROPIC: (Capability.CHAT,),
        ProviderName.DETERMINISTIC: (Capability.EMBEDDING,),
    }
