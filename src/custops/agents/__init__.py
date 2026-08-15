"""The five agents and the state they share (BUILD_SPEC §6, §7).

Each agent has one responsibility and they do not overlap:

* **Supervisor** — what needs to happen? Classify, route, monitor, decide
  completion. Never performs unrestricted business actions.
* **Planner** — how should it be accomplished? Natural language to a structured
  plan.
* **Research** — what do we know, and what supports it? Structured evidence with
  source references, never prose.
* **Execution** — carries out approved actions only, through permission-checked
  tools.
* **Validator** — expected vs actual across every affected system. Never assumes
  a 200 response means the business outcome happened.

The division that matters most is not between agents but between the model and
Python. Routing, budgets, and every safety decision are deterministic functions
in ``routing.py`` and ``budgets.py``; the model classifies, plans, interprets and
drafts. An LLM cannot decide whether to retry, whether evidence was sufficient,
or whether an action needs approval.
"""

from __future__ import annotations
