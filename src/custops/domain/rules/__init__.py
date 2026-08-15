"""Deterministic business rules.

BUILD_SPEC §12 draws the line this package enforces: pricing arithmetic,
proration, thresholds, permission checks, state-transition legality and every
validation comparison expressible as a rule live **here**, in Python, and are
called from Python.

Three properties are deliberate and worth stating:

1. **Pure functions over plain values.** Rules take dataclasses of primitives,
   not ORM instances or database sessions. They can therefore be tested
   exhaustively with no infrastructure, and they cannot accidentally read
   something they were not given.
2. **Not exposed as tools.** Nothing in this package is registered as an MCP
   tool, so no model output can invoke, parameterise around, or override an
   authorization or safety decision.
3. **Recomputable.** The Validator re-runs these functions against the state it
   reads back from the systems of record, and compares. That only works if the
   result is a deterministic function of its inputs.
"""

from __future__ import annotations
