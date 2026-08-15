"""Provider contracts.

Capabilities are separate Protocols because providers genuinely differ in what
they implement. See the package docstring for why that is not over-engineering.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderName(StrEnum):
    """Providers this platform knows how to talk to."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    # Not a provider: a deterministic stand-in used by tests and offline
    # development. Selecting it outside those contexts is rejected — see
    # registry.get_embedding_provider.
    DETERMINISTIC = "deterministic"


class Capability(StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"


class ProviderError(RuntimeError):
    """Base class for provider-layer failures."""


class CapabilityNotSupportedError(ProviderError):
    """Raised when a provider is asked for something it does not implement.

    Raised at configuration time rather than on first use: a system that
    discovers mid-workflow that its embedding provider cannot embed has already
    started work it cannot finish.
    """

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"Provider '{provider}' does not support the '{capability}' capability.")
        self.provider = provider
        self.capability = capability


class ProviderNotConfiguredError(ProviderError):
    """Raised when a provider is selected but its credentials are absent."""

    def __init__(self, provider: str, missing: str) -> None:
        super().__init__(f"Provider '{provider}' selected but {missing} is not set.")
        self.provider = provider
        self.missing = missing


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Vectors plus the provenance needed to trust them later.

    ``model`` and ``dimensions`` travel with the vectors because a stored
    embedding is only comparable to a query embedded by the *same* model at the
    same dimensionality. Recording them is what makes a model change detectable
    instead of silently returning nonsense similarity scores.
    """

    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimensions: int
    provider: str


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors."""

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a batch of texts, preserving input order."""
        ...
