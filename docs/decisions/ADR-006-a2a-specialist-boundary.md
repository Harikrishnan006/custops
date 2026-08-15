# ADR-006: What the A2A boundary is allowed to carry

- **Status:** Accepted
- **Date:** 2026-08-15
- **Phase:** 9
- **Required by:** BUILD_SPEC §9, decision D6 ("the Billing Specialist is a
  genuinely separate A2A participant, not a simulated one")

## Context

D6 puts the Billing Specialist in its own process, reached over A2A. That
settles the *transport*. It does not settle the question that actually
determines whether the boundary is safe: **what may an out-of-process agent
influence?**

Three tempting answers were available, and two of them are wrong.

The specialist holds `Role.BILLING_SPECIALIST` in the permission matrix. That
role can read subscriptions, plans, contracts and invoices. It cannot read
customer records, and no tool in the system exposes negotiated discounts. So the
specialist's view is *structurally* narrower than the orchestrator's — not by
accident, and not by an oversight that a wider matrix would fix.

## Decision

**The specialist's answer is advisory, scoped, and may only ever raise a gate.**

Concretely:

1. Its verdict fields are named for the scope it can see — `billing_eligible`
   and `approval_indicated`, never `eligible` and `requires_approval`.
2. The orchestrator's locally computed figure governs the mutation. A divergence
   is recorded and escalated; it never changes the amount.
3. The specialist may cause an approval gate to appear. It can never remove one.
4. It holds no mutating permission. A test asserts every tool in its role is
   non-mutating.
5. Unreachable is not a blocker. The workflow proceeds on the local
   deterministic calculation and records that it did.

## Why

**1. A remote agent that can lower a gate has moved the approval decision
out of the audited path.** D9 requires three-layer enforcement inside this
system. An agent in another process — potentially another language, another
team's deployment — reporting "no approval needed" and thereby clearing a human
gate would make that enforcement conditional on a service the platform does not
control. The asymmetry (may raise, never lower) is what keeps the specialist
useful without making it trusted.

**2. Its 'no' is informed; its 'yes' is not.** The specialist cannot see whether
the customer is churned or the account suspended, and cannot see a negotiated
discount that would trip the discount threshold. When it says "approval
indicated" it has found a reason the orchestrator should honour. When it says
nothing is needed, it has merely not seen the reasons it cannot see. Treating
those two as symmetric information is the specific error this decision prevents.

**3. Naming the fields honestly is load-bearing, not cosmetic.** A field called
`eligible` on a response object invites exactly one line of code —
`if recommendation.eligible:` — and that line would upgrade a churned customer.
The rename is the cheapest available guard against a future reader who does not
know the role's scope.

**4. Widening the permission matrix would have been the wrong fix.** Granting
`get_customer` to the specialist would make the answer look complete and would
also hand a second process read access to customer records it has no business
holding. The boundary is more valuable than the completeness.

**5. A disagreement about money is a human-review case, not a tiebreak.** Both
sides run the same deterministic arithmetic. If they produce different figures,
they read different state — and neither side can tell from where it stands which
reading is stale. Picking one silently would resolve a real inconsistency by
hiding it. The workflow stops and a person looks.

## Consequences

- The specialist adds a second *read*, not a second algorithm. Its value is
  detecting state divergence between two independent fetches, which is why
  agreement on the figure is asserted in an integration test.
- The platform runs correctly with the specialist switched off. `A2A_ENABLED`
  defaults to `false` for that reason: a main workflow silently depending on an
  optional process being up is an undeclared hard dependency.
- Refusal and unavailability are kept distinct end to end — a failed *task*
  versus an unreachable *agent*. Collapsing them would let a real data problem
  ("this account has no subscription") hide behind "the optional service was
  down".

## Alternatives rejected

**Let the specialist own the pricing decision outright.** Cleanest separation of
concerns on paper. Rejected: the mutation happens on this side of the boundary,
and the side that performs the write must be the side whose reading governs it.

**Simulate A2A in-process behind an interface.** Cheaper, testable, and would
have satisfied a reading of §9. Rejected because it proves nothing — the
failure modes that make agent-to-agent communication hard (the peer is down,
slow, or returns a well-formed lie) do not exist when the peer is a function
call. The subprocess test in
`tests/integration/test_a2a_billing_specialist.py` exists precisely to make the
claim falsifiable.

**Give the specialist `get_customer` so its verdict is complete.** Rejected —
see 4 above.
