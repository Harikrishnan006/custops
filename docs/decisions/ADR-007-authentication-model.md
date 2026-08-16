# ADR-007: Bearer API tokens, and where identity comes from

- **Status:** Accepted
- **Date:** 2026-08-16
- **Phase:** 13
- **Required by:** BUILD_SPEC §17 ("Authentication, role-based authorization…")

## Context

Until Phase 13 the platform had authorization without authentication. The
permission matrix gated agents against tools (Phase 4), the approval authority
policy gated actors against amounts (Phase 7), and every endpoint was open.

The approval endpoint took the actor as a field in the request body. Phase 7's
own docstring said what that meant:

> **Authentication is not implemented** (Phase 13). `actor_user_id` is asserted
> by the caller… this endpoint cannot yet verify that the caller is who they
> claim to be, and the audit trail inherits that limit.

Concretely: anyone who could reach the API could approve a six-figure upgrade as
the finance director, and Phase 12's audit trail would faithfully record the
lie. A perfect audit trail of a forged identity is worse than none, because it
looks like evidence.

## Decision

**Bearer API tokens, hashed at rest, with the authenticated principal as the
sole source of actor identity.**

1. A protected endpoint resolves a `Principal` *before* the handler runs. There
   is no other way for a request to acquire an identity.
2. `actor_user_id` is **removed** from the approval request body, and the schema
   sets `extra="forbid"` so a client still sending it is refused rather than
   silently ignored.
3. Tokens are 256 bits of `secrets.token_urlsafe`, stored as SHA-256 hashes,
   with expiry and revocation. Plaintext exists once, in the output of
   `custops issue-token`.
4. Endpoint authorization is a policy table (`endpoint_authority`), separate
   from both the tool matrix and the approval authority policy.
5. **No authentication-disabled mode.** Tests authenticate through the same
   dependency production uses.

## Why

**1. Tokens rather than passwords.** The `users` table has no credential column
and never had one; callers are services plus a handful of human approvers. A
password store would mean hashing policy, rotation, reset flows and a login
endpoint — a large surface for a platform whose humans only ever approve things.

**2. Tokens rather than OAuth/OIDC.** Disproportionate here, and it would put
the identity provider outside the system under test. §22 excludes
cloud-provider-specific deployment; an external IdP pulls in exactly that.

**3. SHA-256 rather than bcrypt/argon2.** Password KDFs exist to make brute
force expensive against *low-entropy* human-chosen secrets. These tokens carry
256 bits of entropy — there is no dictionary and no feasible search. A slow KDF
would add a per-request cost and buy nothing. The security comes from the
entropy of the token, not the cost of the hash. This reasoning does **not**
transfer to passwords, and the module says so.

**4. Unsalted, deliberately.** Authentication looks a token up *by* its hash;
a per-row salt would force a scan of every row and a hash per candidate. Safe
only because of point 3.

**5. Removing the body field rather than ignoring it.** A field that is accepted
and discarded leaves the caller believing they chose the actor. Refusing the
request tells them they did not.

**6. A separate `api_tokens` table.** One user may hold several credentials with
different lifetimes — a person's CLI and a service acting on their behalf.
Revoking one must not disturb the others, and a token's lifecycle is not
identity.

## Consequences

- **Breaking API change**: `POST /approvals/{id}/decision` no longer accepts
  `actor_user_id`, and every protected endpoint requires `Authorization`.
- Existing integration tests were rewritten to authenticate. Three changed
  meaning, and that is recorded honestly in `PHASE-13-COMPLETION.md` rather than
  papered over: a deactivated approver and an unknown actor are now refused at
  *authentication* (401) instead of at *authority* (403), because identity is
  established earlier than it used to be.
- Deactivating a user ends their access immediately, without anyone having to
  remember which tokens they hold.
- Authentication failures are logged via structlog and are **not** audit events:
  §16 fixes a closed vocabulary of nineteen, and a failed login belongs to no
  execution. Adding a twentieth would break both the spec and the taxonomy test.

## Alternatives rejected

**Keep `actor_user_id` and add authentication alongside it.** Smaller diff, and
would have kept every existing test passing unchanged. Rejected: two sources of
identity is one too many, and the weaker one would win whenever someone forgot
to cross-check them.

**Sign tokens as JWTs.** Stateless verification, no database lookup. Rejected:
revocation then needs a denylist, which reintroduces the lookup while adding
key management and clock-skew handling. Revocation is a hard requirement here;
statelessness is not.

**Per-request HMAC signing.** Stronger against replay over plaintext transport.
Rejected as disproportionate — deployment and TLS termination are out of scope
(§22), and this would complicate every client for a threat the deployment model
does not yet describe.
