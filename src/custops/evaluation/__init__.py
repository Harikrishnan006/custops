"""Evaluating the orchestrator's behaviour (BUILD_SPEC §15).

**This package evaluates the platform; it is not part of it.** Nothing under
``apps/``, ``agents/``, ``mcp/`` or ``domain/`` imports anything here, and
``agent-forge`` is a *dev* dependency — a production install pulls neither it
nor pandas, pyarrow or google-genai. ``tests/unit/test_evaluation_isolation.py``
asserts that boundary rather than trusting it.

**Scoring, judging and regression gating belong to AgentForge** (decision D10).
This package supplies three things AgentForge cannot know about:

* an **adapter** turning a CustOps execution — the Phase 12 trace — into the
  ``AgentTrace`` shape AgentForge scores;
* **datasets** of golden and adversarial cases specific to this workflow (§15);
* **orchestrator-specific metrics** that no generic harness could compute:
  planning accuracy against a ground-truth plan, retrieval precision and recall
  against a labelled evidence set, and whether the Validator actually caught an
  injected cross-system divergence.

Everything else — task success, tool correctness, tool hallucination, step
efficiency, escalation correctness, cost, latency, and the regression gate — is
called, not reimplemented.
"""

from __future__ import annotations
