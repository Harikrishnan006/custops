"""Model provider abstraction (decision D11).

Adding a fourth provider must be a config change plus one adapter — never a
change to business logic. That is the whole test of this package.

**Providers are not interchangeable across capabilities.** Chat completion and
text embedding are separate surfaces, and a provider may implement one without
the other: Anthropic's API offers Messages, Batches, Files and Token Counting —
there is no embeddings endpoint. Modelling providers as a single uniform
interface would either force a fake embeddings implementation for Anthropic
(Rule 6) or silently fail at runtime. Instead each capability is its own
Protocol, and a provider registers only for what it actually supports; asking a
provider for a capability it lacks raises a typed error at configuration time,
not mid-workflow.
"""

from __future__ import annotations
