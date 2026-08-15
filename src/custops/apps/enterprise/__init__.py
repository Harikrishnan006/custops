"""The enterprise systems of record.

CRM, billing and support are **domain modules inside one service**, not three
microservices (decision D5). Three near-identical CRUD services would be padding;
what actually enforces the boundaries is module structure plus MCP tool scoping,
and both of those are real here.

Layering, which matters for Phase 4:

    router (HTTP)  ─┐
                    ├─→  service functions  ─→  models
    MCP tool       ─┘

MCP tools call the *same* service functions the routers call. Tools are a
protocol adapter over this layer, never a second implementation of the business
logic — two implementations would eventually disagree, and the one an agent uses
would be the one nobody tested by hand.

**Mutating operations are deliberately not exposed over HTTP.** They exist as
service functions for the MCP tool layer to call, because that layer
independently verifies an approval record before acting (decision D9). An
unguarded `PATCH /subscriptions/{id}` would be a documented bypass of the entire
approval architecture.
"""

from __future__ import annotations
