# ADR-001: Application code lives under `src/custops/`

- **Status:** Accepted
- **Date:** 2026-08-15
- **Phase:** 1
- **Supersedes:** the flat top-level layout sketched in BUILD_SPEC §4

## Context

BUILD_SPEC §4 describes the repository as a set of top-level directories:
`apps/`, `agents/`, `a2a/`, `mcp/`, `workflows/`, `domain/`, `knowledge/`,
`providers/`, `observability/`, `evaluation/`.

Two of those names collide with distributions this project will install in later
phases:

| Directory (§4) | Installed package | Import name | Needed in |
|---|---|---|---|
| `mcp/` | MCP Python SDK | `mcp` | Phase 4 |
| `a2a/` | `a2a-sdk` | `a2a` | Phase 9 |

Python resolves imports by searching `sys.path` in order, and the repository root
is on `sys.path` in the two situations that matter most: running `python -m ...`
from the root, and pytest's default `prepend` import mode. A top-level directory
named `mcp/` therefore shadows the installed `mcp` SDK. The failure is not a
clean "module not found" — it is an `ImportError` for a *submodule* of a package
that appears to exist, surfacing in Phase 4 with no obvious connection to
directory naming, at the exact moment attention should be on tool design.

The same hazard applies more weakly to every other top-level name: ten
repository-root packages compete for import names with every dependency added
over fourteen phases.

## Decision

All application code lives in one distribution package, `src/custops/`. Every
§4 directory is preserved verbatim *inside* it:

```
src/custops/{apps,agents,a2a,mcp,workflows,domain,knowledge,providers,observability,evaluation}/
```

Nothing is renamed and no boundary is merged or dropped — `custops.mcp` and
`custops.a2a` cannot shadow `mcp` and `a2a`, because they are not top-level
names.

`migrations/`, `tests/`, `infrastructure/` and `docs/` stay at the repository
root: they are not importable application code.

The same reasoning is applied one level down: the Redis client module is
`custops/cache/redis_client.py`, not `redis.py`.

## Consequences

**Positive**

- The import collision is structurally impossible rather than remembered.
- `src/` layout means tests import the *installed* package, so a broken package
  configuration fails in CI instead of being masked by the working directory.
- One place to declare packaging (`[tool.hatch.build.targets.wheel]`).

**Negative**

- Imports gain a `custops.` prefix, and paths in BUILD_SPEC §4 do not match the
  tree literally. This ADR is the reconciliation.
- Requires an editable install (`uv sync`) before tests will run. This bit
  during Phase 1: an editable install created *before* `src/custops/` existed
  produced a package with no modules, and `pytest` failed with
  `ModuleNotFoundError: No module named 'custops'` until `uv sync
  --reinstall-package custops` rebuilt it.

## Alternatives considered

- **Rename only the two colliding directories** (`mcp_server/`, `a2a_agents/`).
  Smaller diff from §4, but leaves ~10 top-level names competing with
  site-packages and diverges from §4's naming anyway.
- **Follow §4 literally.** Rejected: knowingly planting a defect that detonates
  two phases later is not a defensible trade for cosmetic fidelity to a sketch.
