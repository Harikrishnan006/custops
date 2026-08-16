"""Evaluation must not leak into the production path (decision D10, §15).

Two separate guarantees, both easy to lose by accident and neither visible in
ordinary testing — because in a dev environment ``agent_forge`` is installed and
everything imports fine:

* **No production module imports ``custops.evaluation``.** The evaluation layer
  observes the platform; it is not part of it.
* **No production module imports ``agent_forge``.** It is a dev dependency, so a
  production install does not have it — nor pandas, pyarrow or google-genai,
  which it brings with it.

An import added carelessly would work on every developer machine and fail on the
first production install.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "custops"
EVALUATION_PACKAGE = SOURCE_ROOT / "evaluation"

# The CLI dispatches to the evaluation command, but imports it lazily *inside*
# the handler for exactly this reason, so a production `custops seed` never
# touches agent-forge. The AST scan below only inspects module-level imports,
# which is the distinction that matters.
CLI_MODULE = SOURCE_ROOT / "cli.py"


def _production_modules() -> list[Path]:
    return [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if EVALUATION_PACKAGE not in path.parents and path != EVALUATION_PACKAGE
    ]


def _module_level_imports(path: Path) -> set[str]:
    """Top-level imports only.

    A function-local import is deliberate laziness — the CLI uses one so the
    evaluation command exists without the dependency being required at start-up.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)

    return names


def _offenders(prefix: str) -> dict[str, list[str]]:
    """Every production module importing ``prefix``, with what it imported.

    One test scanning all modules rather than one test per module: the
    assertion message still names the offending files, and the suite does not
    gain a hundred-odd cases for a single logical guarantee.
    """
    found: dict[str, list[str]] = {}
    for path in _production_modules():
        hits = sorted(n for n in _module_level_imports(path) if n.startswith(prefix))
        if hits:
            found[str(path.relative_to(SOURCE_ROOT))] = hits
    return found


def test_no_production_module_imports_the_evaluation_layer() -> None:
    offenders = _offenders("custops.evaluation")

    assert not offenders, f"production code imports the evaluation layer: {offenders}"


def test_no_production_module_imports_agent_forge() -> None:
    """agent-forge is a dev dependency; production does not install it."""
    offenders = _offenders("agent_forge")

    assert not offenders, f"production code imports agent_forge at module level: {offenders}"


def test_agent_forge_is_not_a_production_dependency() -> None:
    """The declaration itself, not just the imports.

    Checked in the manifest because that is what a production install reads.
    """
    import tomllib

    manifest = tomllib.loads(
        (SOURCE_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    runtime = manifest["project"]["dependencies"]

    assert not [dep for dep in runtime if "agent-forge" in dep or "agent_forge" in dep]
    assert any("agent-forge" in dep for dep in manifest["dependency-groups"]["dev"])


def test_the_cli_reaches_evaluation_without_importing_it_up_front() -> None:
    """`custops seed` must work on an install that has no agent-forge."""
    imports = _module_level_imports(CLI_MODULE)

    assert not any(name.startswith("custops.evaluation") for name in imports)
    assert "evaluate" in CLI_MODULE.read_text(encoding="utf-8")


def test_the_evaluation_layer_may_import_production_code() -> None:
    """The dependency runs one way only.

    Evaluation reads the permission matrix and the workflow state — that is the
    point. The prohibition is on production depending on evaluation.
    """
    runner = EVALUATION_PACKAGE / "runner.py"

    assert any(name.startswith("custops.") for name in _module_level_imports(runner))
