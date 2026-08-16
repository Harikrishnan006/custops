# Phase 14 — completion report

**Date:** 2026-08-16
**Scope:** CI/CD, final documentation, architecture diagrams
**Status:** Code and documentation complete. **Three verification steps could not
be executed in this environment** — see "What was not verified", which is the
most important section of this report.

---

## What was not verified, and why

This phase's centrepiece — running the 160 pending integration tests against a
real PostgreSQL+pgvector service container — **has not run.** Three environmental
blockers, none of them defects in the code:

| Blocker | Consequence |
|---|---|
| **No git remote on this repository** (`git remote -v` is empty) | The workflows cannot be pushed, so GitHub Actions has never executed. No CI run URLs exist. |
| **No Docker** (excluded by standing constraint) | `docker build` and `docker compose config` could not be run locally. |
| **Local PostgreSQL lacks the `custops` role and pgvector** | `tests/integration` still skips, exactly as in every prior phase. |

**The `integration`, `e2e` and `container` jobs are therefore written but
unexecuted.** They are the plan as approved, and the YAML parses, but "it parses"
is not "it passes". Anyone reading this should treat those three jobs as
unproven until a first CI run is green.

That distinction is the whole point of how this project has reported results:
a skip says "not exercised", and claiming otherwise would be the one failure
mode the testing discipline exists to prevent.

---

## What was built

### CI (`.github/workflows/ci.yml`)

Six parallel jobs, replacing a single sequential one:

| Job | Runs |
|---|---|
| `quality` | `ruff`, `mypy --strict` — **first**, so a type error does not wait behind a suite |
| `unit` | `pytest tests/unit` (includes migration, security and documentation suites) |
| `integration` | migrations → seed → `pytest tests/integration`, against `pgvector/pgvector:0.8.6-pg17` + `redis:8.10` |
| `e2e` | Chromium via `playwright install --with-deps`, separated so a flaky browser cannot obscure a green integration run |
| `package` | `uv build`, install the wheel into a clean venv, import from a different working directory |
| `container` | `docker compose config`, assert **exactly three services**, `docker build` the API image |

Every install uses **`uv sync --locked`**: a lockfile CI ignores is not a
lockfile.

`evaluation.yml` is untouched. One regression mechanism, owned by AgentForge —
duplicating it in `ci.yml` would create two gates that could disagree.

**No authentication bypass.** The integration job runs the real fixtures, which
mint genuine tokens through `custops.apps.api.security.issuance` and insert
genuine hashes. Migrations and `custops seed` are the normal paths, not a
CI-only database state.

### Documentation

`README.md` and `docs/architecture/overview.md` were both last touched at
`6092346` — **Phase 3**. Ten phases of architecture were undocumented, and
worse, Phase 13 had silently invalidated the README: a stranger following it
would hit a 401 with no instruction anywhere on issuing a token, which is
precisely the "undocumented step" §21.8 forbids.

- **README** gains a full authenticated walkthrough: issue a token, start a
  workflow, read the trace, approve a paused run, revoke the credential. Stale
  test counts corrected; the evaluation gate documented as dev-only.
- **Architecture overview** rewritten to the delivered system.
- **`docs/PHASE-04-COMPLETION.md`** written retrospectively — every other phase
  had one, and the gap was found by the new documentation test, not by memory.
- **Rule 23 checklists** added to Phases 9–13. They were present through Phase 8
  and then quietly stopped; Rule 23 is a standing requirement, not a per-phase
  option.

### Four diagrams

Mermaid, in-repo, reviewable in a diff — a binary image cannot be reviewed in a
pull request and drifts from the code with nothing to catch it.

1. **The shape of a run** — the real graph topology, read from `graph.py` rather
   than drawn from memory: conditional edges, the interrupt, and the
   budget-spending `retry`/`replan` nodes.
2. **Three protocols** — LangGraph vs MCP vs A2A, including the specialist's
   arrow *back into* the MCP layer.
3. **Security boundaries** — the four independent gates in one place. This is
   the diagram that earns its keep: nothing else shows authentication, endpoint
   authority, tool permission and approval together.
4. **Trace assembly** — three writers, one recorder, one ordered timeline.

A deployment diagram was deliberately not drawn (out of scope, §22), nor an ER
diagram (the models are self-documenting and it would rot).

### Documentation tests

`tests/unit/test_documentation_consistency.py` — 26 tests pinning the claims
that go stale silently: every phase has a completion document, every referenced
ADR exists, ADR numbering has no gaps, internal links resolve, the README only
documents CLI commands the parser actually has, and the architecture document
mentions the subsystems that exist. Structure and references, never prose —
asserting on wording would make every edit a failure and teach people to weaken
the test.

---

## Versioning — no change, and why

You asked me to check rather than assume. **BUILD_SPEC contains no release,
tag, version or semver requirement anywhere.** Phase 14 is *"CI/CD, final
documentation, architecture diagrams"* — nothing more. The only occurrences of
"deliverable" concern ADR-004.

So the version stays at `0.1.0` and **no tag is created**. Cutting `v1.0.0`
would be inventing a requirement, and doing it while three CI jobs have never
run would be worse than inventing it.

---

## Verification

Executed locally:

```
ruff check                        clean
mypy --strict                     clean, 192 files
pytest                            662 passed, 160 skipped
custops evaluate (regression gate) PASSED — all six gated metrics [=], exit 0
uv build                          sdist + wheel
wheel installed in a clean venv   imports from an unrelated cwd; console script on PATH
```

**Not executed** (see above): `pytest tests/integration`, `docker build`,
`docker compose config`, and any GitHub Actions run.

Test delta from `8ec921d`: **+26 passing** (the documentation suite), 160
skipped — unchanged, because nothing here could un-skip them without the
infrastructure.

---

## Is the project ready for the release BUILD_SPEC specifies?

**BUILD_SPEC specifies no release.** On the narrower question — is the system
ready to be called finished — the honest answer is *not yet*, for one reason:

**160 tests have never executed anywhere.** They are written, marked, and skip
with the precise blocker named. Every one of them covers something the unit
suite structurally cannot: that events reach PostgreSQL, that the A2A specialist
answers over a real socket, that authentication works against real token rows,
that the adapter reads a genuinely executed workflow. Until a CI run turns them
green, the system is well-tested in the parts that need no infrastructure and
**unproven in the parts that do**.

The first CI run is likely to surface real defects — that is the point of
running it, and the stale `HEAD_REVISION` bug found in an earlier phase is
evidence that this class of latent failure exists here.

---

## Next step

Add a remote and push. The `integration` job will then either go green — at
which point 160 tests move from "pending" to "passing" and the project is
genuinely done — or it will fail, and those failures are the remaining Phase 14
work.
