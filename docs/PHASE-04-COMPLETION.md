# Phase 4 — completion report

**Date:** 2026-08-15 (commit `f6a2ea7`)
**Scope:** MCP server, typed tools, permission matrix, approval enforcement
**Status:** Code complete.

> **Written retrospectively during Phase 14.** Every other phase left a
> completion record; this one did not, and the gap was found by the
> documentation-consistency test added in Phase 14. Reconstructed from commit
> `f6a2ea7` and the code as it stands — not from memory, and not invented.

---

## What was built

| Piece | Where |
|---|---|
| Permission matrix | `mcp/permissions/matrix.py` |
| The single tool path | `mcp/tools/runtime.py` (`execute_tool`) |
| Approval verification | `mcp/tools/approval.py` (`verify_approval`) |
| Typed tool schemas and results | `mcp/tools/schemas.py`, `results.py` |
| Tool handlers | `mcp/tools/enterprise.py` |
| MCP server | `mcp/server/app.py` |
| `approvals`, `tool_calls` tables | migration `0005` |

---

## The decisions that mattered

**The SDK surface was introspected, not recalled.** MCP 2.0 has no `FastMCP` —
the server class is `MCPServer` from `mcp.server`. A recalled import would have
failed at runtime, and this is the phase that established Rule 24 as a working
habit rather than an aspiration.

**The enforcement funnel is the design.** Permission, approval, audit and error
conversion happen in `execute_tool`, not in tool bodies, so a tool cannot forget
a check it never performs. Adding a twelfth tool cannot ship without an audit
row, because writing the audit row is not the tool's job.

**Approval verification requires four things at once**: a row for this
`execution_id`, for this action, scoped to the exact entity, with status exactly
`APPROVED`, not already consumed. Each conjunct closes a specific hole — without
execution scoping one approval authorises every later workflow; without the
entity check, approval to upgrade Acme authorises upgrading Globex; without the
exact status test a `PENDING` row reads as not-rejected; without consumption a
retry loop replays one human decision into many mutations.

**Permission and approval are separate gates on purpose.** Permission is
capability (Research may never call `update_subscription`, approval or no
approval); approval is authorisation for one particular act. Merging them would
let a mutating capability act without a human, or let an approval substitute for
a capability never granted.

**Savepoint placement.** Approval verification and the handler share one
savepoint, so a handler that raises part-way cannot leave a half-applied change
behind or an approval marked spent for work that never happened. Audit rows are
written *outside* it, so a failed attempt survives the rollback of its own
changes.

**Tool errors are typed codes, never exceptions.** An agent receiving a
traceback cannot distinguish "customer not found" (replan) from "database
unreachable" (retry) from "not allowed" (escalate) — and a driver message leaks
connection details into model context. Unknown failures default to
non-retryable: guessing that an unknown failure is transient is how a workflow
retries a corruption into place.

---

## Known gaps at the time

- Nine of eleven tools implemented; `create_refund` and `send_notification` were
  deferred as they belong to workflows outside D3's single shipped workflow.
- `update_entitlement` / `get_entitlement` arrived in Phase 8 with the portal.
- Approval *records* existed, but nothing created them until Phase 7 built the
  gate and the API. Layer 3 was therefore testable in isolation before layers 1
  and 2 existed — which is exactly how D9 was verified: call a mutating tool
  directly, bypassing the graph entirely, and assert refusal.

---

## What you should be able to explain (Rule 23)

- Why permission and approval are two gates and not one.
- What each of the four conjuncts in `verify_approval` prevents, with a concrete
  attack for each.
- Why the handler runs inside a savepoint but the audit row is written outside
  it.
- Why an unknown tool failure defaults to non-retryable.
- Why a tool returning a typed error code beats raising an exception, in terms
  of what the agent does next.
- How you would prove layer 3 works without running the graph at all.
