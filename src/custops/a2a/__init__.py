"""Agent-to-agent communication (BUILD_SPEC §9, decision D6).

Three protocols, three jobs, and they do not overlap:

* **LangGraph** — workflow and state orchestration, inside one process.
* **A2A** — one agent asking another agent, across a process boundary.
* **MCP** — an agent reaching a tool.

§9 warns against implementing A2A as "two of my services calling each other over
HTTP". What makes this real rather than that:

* The Billing Specialist runs in **its own process**, on its own port, startable
  with nothing else running.
* It publishes an **agent card** at the spec's well-known URI, so a client
  discovers its capabilities rather than being configured with them.
* It **owns its own tool access**. The orchestrator does not fetch billing data
  and hand it over; the specialist reads what it needs through its own
  MCP-scoped, read-only role.
* The orchestrator **degrades gracefully** when it is absent. A dependency that
  cannot be down is not a separate system.

Note this package is ``custops.a2a``, not top-level ``a2a`` — so it does not
shadow the SDK's import name. That is the collision ADR-001 was written to
avoid, and this is the phase where it would have bitten.
"""

from __future__ import annotations
