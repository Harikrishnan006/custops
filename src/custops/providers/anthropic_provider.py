"""Anthropic adapter.

**Anthropic does not offer a text-embedding endpoint.** The API surface is
Messages, Batches, Files and Token Counting; there is no `/v1/embeddings`.

That fact is the reason this package models capabilities separately. The honest
options were: implement `embed()` by calling some other vendor behind
Anthropic's name (a lie in the trace and in the audit log), implement it to
return zeros (a silent corruption of every similarity score), or declare the
capability unsupported and fail loudly at configuration time. This module does
the third.

Chat completion — which Anthropic obviously does support — arrives in Phase 5
with the LangGraph runtime that needs it. Declaring the class now, with the
capability boundary stated, is what stops Phase 3 from quietly assuming every
provider can do everything.
"""

from __future__ import annotations

from custops.providers.base import (
    Capability,
    CapabilityNotSupportedError,
    EmbeddingResult,
    ProviderName,
)

# Current model identifiers, verified against Anthropic's published model list
# rather than recalled (Rule 24). Opus 5 is the default for reasoning-heavy
# agent work; Haiku 4.5 is the cheap tier for classification-shaped tasks.
DEFAULT_CHAT_MODEL = "claude-opus-5"
FAST_CHAT_MODEL = "claude-haiku-4-5"


class AnthropicEmbeddingProvider:
    """Declared, and deliberately unsupported.

    Present so that selecting Anthropic for embeddings fails with an
    explanation instead of a ``KeyError`` in the registry.
    """

    @property
    def model(self) -> str:
        raise CapabilityNotSupportedError(ProviderName.ANTHROPIC, Capability.EMBEDDING)

    @property
    def dimensions(self) -> int:
        raise CapabilityNotSupportedError(ProviderName.ANTHROPIC, Capability.EMBEDDING)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        raise CapabilityNotSupportedError(ProviderName.ANTHROPIC, Capability.EMBEDDING)
