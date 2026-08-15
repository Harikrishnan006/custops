"""Domain layer: models, deterministic business rules, policies.

BUILD_SPEC §12 draws a hard line here. ``domain/rules`` and ``domain/policies``
(Phase 2 onward) hold pricing arithmetic, thresholds, authorization and state
transition legality. They are called from Python and are never exposed as tools
an LLM can influence, so no model output can override a safety or authorization
decision.
"""

from __future__ import annotations
