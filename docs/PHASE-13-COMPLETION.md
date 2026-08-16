# Phase 13 — completion report

**Date:** 2026-08-16
**Scope:** Security hardening (§17)
**Status:** Code complete. Authentication, authorization and the §17 prohibitions
verified locally over real HTTP; authentication against real database rows
pending PostgreSQL.

---

## A numbering note

The phase table's **Phase 13** is *Security hardening*; its spec content is
**§17**. **§13** is *Human-in-the-loop*, delivered in Phase 7 (`246a34b`). This
report addresses §17, with §13's approval work as an input — §17's checklist
names "approval enforcement" among its items.

---

## What the audit found

| §17 requirement | State before | Action |
|---|---|---|
| Tool-level permissions | ✅ Phase 4 | untouched |
| Approval enforcement (D9) | ✅ Phase 7 | untouched |
| Audit logging | ✅ Phase 12 | untouched |
| Environment-based secrets | ✅ `SecretStr`, `.gitignore` | guard tests added |
| Input validation | ✅ Pydantic | unchanged |
| **Role-based authorization** | ⚠️ approvals only | extended to every endpoint |
| **Safe action boundaries** | ⚠️ structural, untested | five guard tests added |
| **Authentication** | ❌ **absent** | built |

The finding that mattered: **the approval endpoint took the actor as a request
body field.** Phase 7's own docstring flagged it — *"`actor_user_id` is asserted
by the caller… the audit trail inherits that limit."* Anyone who could reach the
API could approve a six-figure upgrade as the finance director, and Phase 12's
audit trail would faithfully record the lie. A perfect audit trail of a forged
identity is worse than none, because it looks like evidence.

---

## What was built

| Piece | Where |
|---|---|
| Credential model | `domain/models/credential.py` |
| Migration | `migrations/versions/0007_api_tokens.py` |
| Token algebra (mint/hash/compare/parse/validity) | `apps/api/security/tokens.py` |
| Issuance and revocation | `apps/api/security/issuance.py` |
| The authentication dependency | `apps/api/security/principal.py` |
| Endpoint authorization policy | `domain/policies/endpoint_authority.py` |
| `custops issue-token` / `revoke-token` | `cli.py` |

**Identity now has exactly one source.** `actor_user_id` is gone from the
approval request body, and the schema sets `extra="forbid"` so a client still
sending it is *refused* rather than silently ignored — a caller who believes
they are choosing the actor should be told they are not.

**Reading is broad, acting is narrow.** A viewer may inspect any trace, which is
what makes the audit trail useful. Starting a workflow reaches billing, CRM and
the legacy portal, so it requires `operator`; deciding an approval requires an
approving role. A test asserts every mutating action's role set is a strict
subset of the readers'.

---

## Three decisions worth recording

**SHA-256, unsalted — and why that is not a mistake.** Password KDFs exist to
make brute force expensive against *low-entropy* human-chosen secrets. These
tokens are 256 bits of `secrets.token_urlsafe`; there is no dictionary and no
feasible search. Authentication also looks a token up *by* its hash, which a
per-row salt would make impossible without scanning every row. The reasoning
does not transfer to passwords, and the module says so at length. ADR-007.

**No authentication-disabled test mode.** Integration tests mint real tokens and
insert real hashes; unit tests substitute only the *session*, leaving the real
dependency in the request path. A bypass flag is exactly the thing that
eventually ships enabled.

**Authentication failures are logged, not audited.** §16 fixes a closed
vocabulary of nineteen events; a failed login belongs to no execution and
describes no workflow step. It goes to structlog with a reason code. The
taxonomy remains at exactly 19 and its test still passes.

---

## Three existing tests changed meaning — recorded, not papered over

Authentication establishes identity *earlier* than the old body field did, so
three approval tests now assert a different (and stronger) refusal:

| Test | Before | After |
|---|---|---|
| viewer decides | 403 `no_approval_role` | 403 `insufficient_role` — refused at the endpoint, before the approval record is touched |
| deactivated approver | 403 `actor_inactive` | **401** — cannot authenticate at all; their existing tokens stop working without anyone remembering which they hold |
| unknown actor | 403 `actor_not_found` | **401** — there is no longer any way to *name* an actor; the equivalent attack is presenting a token nobody issued |

A fourth improved: *"the original decision survives a second attempt"* now uses
the **finance** approver rather than the same user, because if seniority could
reopen a settled decision, "already decided" would mean "decided by someone
junior".

---

## Verification

```
ruff check      clean
mypy --strict   clean, 191 files
pytest          636 passed, 160 skipped   (was 549 / 160 at fa87267)
```

Baseline measured with `git stash`, not recalled. **+87 passing, +0 pending**:
86 new security tests, plus one auto-generated migration/model consistency case
for the new `api_tokens` table.

| Suite | Tests |
|---|---:|
| `test_security_tokens.py` | 30 |
| `test_security_principal.py` | 23 |
| `test_security_prohibitions.py` | 17 |
| `test_endpoint_authority.py` | 16 |

Against your checklist, all verified **locally**:

- unauthenticated protected request → **401**, with a `WWW-Authenticate` challenge
- authenticated but wrong role → **403** `insufficient_role`
- expired token → 401; revoked token → 401; deactivated user → 401
- the 401 body does not reveal *which* — that would tell an attacker whether a
  guessed token ever existed
- a body-supplied `actor_user_id` cannot override the principal, and the schema
  rejects it outright
- approval authority still applies *after* endpoint authorization: a plain
  approver reaching the endpoint still cannot sign off a six-figure upgrade
- audit actor identity comes from the principal
- no plaintext token in any store, log, repr or response
- all five §17 prohibitions assert

---

## Infrastructure-dependent verification (160 pending, unchanged)

No *new* pending tests were added. Authentication against real rows is covered
by the **rewritten** integration suites, which mint genuine tokens and insert
genuine hashes — pending **PostgreSQL unusable: role `custops` does not exist
(`InvalidPasswordError`)**. Those tests were rewritten rather than weakened: they
now authenticate through the production dependency, and would fail on 401 if the
guards were removed.

---

## Defects found along the way

**An over-broad guard test.** The first version of the shell-execution check
matched `compile` as an *attribute*, flagging `re.compile` and LangGraph's
`graph.compile`. Both are unrelated. Left as written it would have had to be
silenced with exemptions — which is how a guard test becomes noise people learn
to ignore. Narrowed to bare-name builtins.

**A leaked bulk edit.** Removing `actor_user_id` mechanically replaced a call in
`test_the_original_decision_survives_a_second_attempt` with the wrong fixture,
which would have used an undefined name. Caught and rewritten to the stronger
finance-approver form above.

---

## Still outstanding

- Phase 14: CI/CD, final documentation, architecture diagrams.
- 160 tests pending PostgreSQL / Chromium.
- Not in §17 and deliberately not done: rate limiting, TLS termination, CORS
  policy, secret rotation. Deployment is out of scope (§22).

---

## Rule 23 — what you should be able to explain

1. Why an audit trail of a forged identity is worse than no audit trail.
2. Why SHA-256 unsalted is correct here and would be wrong for passwords.
3. Why `actor_user_id` was removed rather than ignored.
4. Why a 401 body does not say whether the token was expired, revoked or unknown.
5. The difference between endpoint authority and approval authority, and why
   both apply.
6. Why there is no authentication-disabled test mode.
7. Why deactivating a user ends access without revoking their tokens.
8. Which three existing tests changed meaning, and why that was an improvement
   rather than a regression.
