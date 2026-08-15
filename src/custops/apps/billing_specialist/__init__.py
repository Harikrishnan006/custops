"""The Billing Specialist agent — a separate process (D6, §9).

Runs on its own port with its own entry point, startable with nothing else
running. It reads billing data through **its own** MCP tool access under the
read-only ``billing_specialist`` role, so the orchestrator never fetches data on
its behalf and never reaches into billing tools directly.

It reasons; it does not mutate. The permission matrix grants it no mutating
tool, and a unit test asserts that. Every state change in this system still
travels the MCP path with approval enforcement — A2A adds a second opinion, not
a second way to write.
"""

from __future__ import annotations
