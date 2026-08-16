"""Golden and adversarial evaluation cases (BUILD_SPEC §15).

Two halves that must stay in step:

* ``golden_tasks.json`` — what a correct run looks like, in AgentForge's
  ``GoldenTask`` shape, loaded with AgentForge's own ``load_golden_tasks``.
* ``scenarios.py`` — the CustOps execution records that produce those runs.

The scenarios are **CustOps rows**, not AgentForge traces. They are fed through
the real adapter at evaluation time, so the adapter is exercised by every
evaluation rather than bypassed by hand-written output in the target format.
"""

from __future__ import annotations
