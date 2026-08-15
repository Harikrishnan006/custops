# Phase 7 — completion report

**Date:** 2026-08-15
**Scope:** Human-in-the-loop — approval API and three-layer enforcement
**Status:** Code complete. Authority rules fully verified; the loop itself pending infrastructure.

---

## The three layers, and what changed

| Layer | Mechanism | Phase |
|---|---|---|
| 1 | Graph routes to `approval_gate` and calls `interrupt()` | 5 |
| 2 | **The approval API records the decision with actor and timestamp** | **7** |
| 3 | The MCP mutating tool independently verifies before acting (D9) | 4, strengthened here |

Layer 2 was the gap. Two things also changed in the layers that existed.

**A duplicate write path was removed.** The `approval_gate` node previously
wrote the decision when it resumed. §13 puts recording in the API, so the node
now *reads the row back* and derives its state from it. There is exactly one
authority on what a human decided; before this, the graph's view and the audit
record could diverge, and the divergence would have favoured whichever ran last.

**Layer 3 gained a freshness check.** An approval decided long ago is refused by
`verify_approval` even when its status still says `approved`. Checked in the tool
layer rather than trusting a sweeper job to have flipped the status — layer 3's
whole point is that it verifies for itself.

---

## What was built

| Piece | Where |
|---|---|
| Authority, decidability and freshness rules | `domain/policies/approval_authority.py` |
| `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/decision` | `apps/api/routers/approvals.py` |
| Transport schemas | `apps/api/schemas/approval.py` |
| Approver users and roles | `domain/seed.py` |

The decision endpoint reuses `WorkflowRunner.resume()` and the graph's own
interrupt mechanism — no second approval path was created.

**Ordering inside the endpoint is deliberate:** decidability, then authority,
then write, then resume. Decidability first so a second approver racing on an
already-decided request gets the accurate reason rather than a role complaint.
The decision is committed *before* the workflow resumes, so a crash between them
leaves a recorded decision and a resumable workflow — never a workflow that
acted on a decision nobody recorded.

---

## Authentication is not implemented — and this matters

`actor_user_id` is **asserted by the caller, not proven**. This endpoint enforces
*authorisation* (does this user exist, are they active, do they hold an approving
role?) but cannot yet verify that the caller is who they claim to be.

Authentication is Phase 13. Until then the audit trail inherits that limit: it
records who *was named* as the approver, which is weaker than recording who
approved. This is stated in the module docstring rather than left for someone to
discover.

---

## Verified (305 unit tests, ruff + mypy --strict clean over 130 files)

25 new tests over pure rules, no database required:

- **Authority fails closed** at every step — unknown actor, deactivated account,
  no approving role, and a role that is insufficient for the amount. A
  deactivated user holding `approver` is refused: a role alone must not confer
  authority on a closed account.
- **Elevated authority above a threshold**, mirroring seeded policy DIS-002, with
  the boundary strictly greater (consistent with the approval thresholds
  elsewhere in the system).
- **Decidability**: an approved or rejected request cannot be re-decided;
  consumption outranks status as the reason; any *other* status — including one
  added later — is refused by default rather than accepted.
- **Freshness**: an old decision is stale, the window boundary is exact, and an
  approval with no decision timestamp is stale because it cannot show it is
  current.
- Every refusal carries a distinct code and a message.

## Not verified — pending infrastructure

**119 integration tests pending, 18 new.** The loop has never run. Unproven:

- That a paused workflow's approval appears in the API, that deciding it resumes
  the graph, and that rejection escalates without executing.
- That the graph reads the decision back from the row rather than assuming it.
- That an unauthorised attempt leaves the record untouched.
- That the audit event carries the actor.

The new tests cover cross-decision replay (deciding twice, the original decision
surviving), consumed-approval refusal, staleness refused by layer 3, and
authority refusals for viewer / deactivated / unknown actors.

Cross-execution and cross-entity misuse were already covered by Phase 4's
`test_approval_enforcement.py`, which is untouched.

---

## Rule 23 — what you should be able to explain

1. Why three layers, and which one would still refuse if the other two were
   bypassed.
2. Why the node no longer writes the decision.
3. Why the decision is committed before the workflow resumes.
4. Why decidability is checked before authority.
5. Why a deactivated user with an approver role is refused.
6. Why freshness is checked in the tool layer rather than by a sweeper job.
7. Why an approval with no `decided_at` is treated as stale.
8. Why an unrecognised status is refused rather than accepted.
9. What `actor_user_id` does and does not prove today.
10. Why `403` rather than `401` on an authority refusal.
