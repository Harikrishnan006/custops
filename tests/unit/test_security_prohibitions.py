"""§17's five prohibitions, asserted as a set.

    "An agent must never: modify arbitrary database records, bypass
    authorization, bypass approval, access secrets, or execute shell commands."

Each is already prevented structurally — by the MCP chokepoint, the permission
matrix, D9's approval verification, and the absence of any shell path. What was
missing until now is anything that *fails* if one of those properties is lost.
A guarantee nothing checks is a guarantee that quietly expires.

These are structural tests. They read source and configuration rather than
running a workflow, which is what lets them run everywhere and fail fast.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "custops"

# The evaluation layer is dev-only tooling, not an agent path. Benchmarks and
# tests legitimately use subprocesses (Phase 9 starts a real A2A process).
AGENT_REACHABLE = [
    path
    for path in SOURCE_ROOT.rglob("*.py")
    if "evaluation" not in path.parts
]


def _calls_named(path: Path, names: set[str]) -> set[str]:
    """Function calls made in a module, by simple or dotted name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id in names:
            found.add(target.id)
        elif isinstance(target, ast.Attribute) and target.attr in names:
            found.add(target.attr)
    return found


# ------------------------------------------------- never execute shell commands


def test_no_agent_reachable_module_executes_a_shell_command() -> None:
    """The prohibition with the least structural protection.

    Nothing stops someone importing subprocess into a tool handler; only this
    test would notice. Playwright launches a browser, but through its own API
    inside the provisioning boundary — not by handing a string to a shell.
    """
    forbidden = {"system", "popen", "run", "call", "check_output", "check_call", "Popen"}
    offenders: dict[str, set[str]] = {}

    for path in AGENT_REACHABLE:
        source = path.read_text(encoding="utf-8")
        # `run` and `call` are ordinary words; only flag them alongside an
        # actual import of a process-spawning module.
        if not any(module in source for module in ("subprocess", "os.system", "pty", "popen")):
            continue
        hits = _calls_named(path, forbidden)
        if hits:
            offenders[str(path.relative_to(SOURCE_ROOT))] = hits

    assert not offenders, f"shell execution reachable from an agent path: {offenders}"


def _builtin_calls(path: Path, names: set[str]) -> set[str]:
    """Bare-name calls only — the builtins, not same-named methods.

    ``re.compile`` and LangGraph's ``graph.compile`` are attribute calls on
    unrelated objects. Matching on the attribute name flagged all three and
    would have had to be silenced, which is how a guard test becomes noise
    people learn to ignore.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in names
    }


def test_no_agent_reachable_module_evaluates_source_at_runtime() -> None:
    """`eval`/`exec` are shell execution wearing a different hat."""
    offenders: dict[str, set[str]] = {}

    for path in AGENT_REACHABLE:
        hits = _builtin_calls(path, {"eval", "exec", "compile"})
        if hits:
            offenders[str(path.relative_to(SOURCE_ROOT))] = hits

    assert not offenders, f"runtime source evaluation reachable from an agent path: {offenders}"


# ------------------------------------------------------------ never bypass authz


def test_every_tool_has_a_permission_policy() -> None:
    """A tool with no matrix entry is denied, not defaulted — but a tool
    *missing* from the matrix would be an unreachable tool, which is its own
    kind of defect."""
    from custops.mcp.permissions.matrix import PERMISSION_MATRIX, ToolName

    assert {str(tool) for tool in ToolName} == set(PERMISSION_MATRIX)


def test_no_module_outside_the_tool_runtime_calls_a_tool_handler_directly() -> None:
    """The MCP chokepoint is only a chokepoint if nothing walks around it.

    Calling ``handlers.update_subscription(...)`` directly would skip
    permission, approval and audit in one move.
    """
    handlers_module = SOURCE_ROOT / "mcp" / "tools" / "enterprise.py"
    runtime_module = SOURCE_ROOT / "mcp" / "tools" / "runtime.py"

    mutating = {"update_subscription", "update_crm", "create_refund", "send_notification"}
    offenders: dict[str, set[str]] = {}

    for path in AGENT_REACHABLE:
        if path in (handlers_module, runtime_module):
            continue
        hits = _calls_named(path, mutating)
        if hits:
            offenders[str(path.relative_to(SOURCE_ROOT))] = hits

    assert not offenders, f"mutating tool handlers called outside the runtime: {offenders}"


# ---------------------------------------------------------- never bypass approval


def test_every_mutating_tool_declares_an_approval_action() -> None:
    """D9's layer 3 verifies an approval whose action name comes from here.

    A mutating tool with no declared action would verify against the tool's own
    name, quietly requiring an approval nothing ever creates — or worse, one
    that some other workflow did.
    """
    from custops.mcp.permissions.matrix import PERMISSION_MATRIX

    missing = [
        name
        for name, policy in PERMISSION_MATRIX.items()
        if policy.mutating and not policy.approval_action
    ]

    assert not missing, f"mutating tools without an approval action: {missing}"


def test_only_execution_and_admin_hold_mutating_permissions() -> None:
    """Every other agent role — including the out-of-process specialist — must
    be structurally unable to change state."""
    from custops.mcp.permissions.matrix import PERMISSION_MATRIX, Role

    for name, policy in PERMISSION_MATRIX.items():
        if not policy.mutating:
            continue
        assert policy.allowed_roles <= {Role.EXECUTION, Role.ADMIN}, name


# --------------------------------------------------------------- never see secrets


def test_no_tool_output_schema_exposes_a_credential_field() -> None:
    """Tool results reach an agent's context and the audit trail."""
    from custops.mcp.tools import schemas as tool_schemas
    from custops.observability.redaction import SECRET_KEYS

    offenders: dict[str, list[str]] = {}
    for name in dir(tool_schemas):
        candidate = getattr(tool_schemas, name)
        fields = getattr(candidate, "model_fields", None)
        if not isinstance(fields, dict):
            continue
        leaks = [field for field in fields if field.lower() in SECRET_KEYS]
        if leaks:
            offenders[name] = leaks

    assert not offenders, f"tool schemas expose credential fields: {offenders}"


def test_settings_hold_secrets_as_secretstr() -> None:
    """A plain ``str`` would render in a repr, a log line or a traceback."""
    from pydantic import SecretStr

    from custops.config import PortalSettings, PostgresSettings

    assert PostgresSettings.model_fields["password"].annotation is SecretStr
    assert PortalSettings.model_fields["password"].annotation is SecretStr


def test_no_secret_is_hardcoded_in_source() -> None:
    """§17: no hardcoded secrets. Defaults that are obviously placeholders are
    permitted — `change-me-locally` is not a secret, it is an instruction."""
    placeholders = {"change-me-locally", "custops", "***", ""}
    offenders: list[str] = []

    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if name != "SecretStr":
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value not in placeholders
                ):
                    offenders.append(f"{path.relative_to(SOURCE_ROOT)}: {arg.value[:12]}…")

    assert not offenders, f"hardcoded secrets: {offenders}"


def test_env_is_ignored_and_the_example_is_committed() -> None:
    """§17 names both halves explicitly."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in gitignore
    assert "!.env.example" in gitignore
    assert (REPO_ROOT / ".env.example").is_file()


def test_no_env_file_is_tracked_by_git() -> None:
    """The ignore rule only helps for files git is not already tracking."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    leaked = [name for name in tracked if name == ".env" or name.startswith(".env.")]

    assert leaked == [".env.example"], f"unexpected env files tracked: {leaked}"


# --------------------------------------------- never modify arbitrary records


def test_agent_reachable_code_never_executes_raw_sql() -> None:
    """Typed tools over named entities are the boundary.

    A raw ``text("UPDATE …")`` reachable from an agent path would be exactly
    the "modify arbitrary database records" §17 forbids. The knowledge layer
    uses ``text()`` for pgvector operators, so this scopes to statements that
    write.
    """
    writing = ("update ", "delete ", "insert ", "drop ", "alter ", "truncate ")
    offenders: dict[str, list[str]] = {}

    for path in AGENT_REACHABLE:
        source = path.read_text(encoding="utf-8")
        if "text(" not in source:
            continue
        hits = [
            line.strip()[:60]
            for line in source.splitlines()
            if "text(" in line and any(verb in line.lower() for verb in writing)
        ]
        if hits:
            offenders[str(path.relative_to(SOURCE_ROOT))] = hits

    assert not offenders, f"raw write SQL reachable from an agent path: {offenders}"


@pytest.mark.parametrize(
    "prohibition",
    [
        "modify arbitrary database records",
        "bypass authorization",
        "bypass approval",
        "access secrets",
        "execute shell commands",
    ],
)
def test_the_specification_prohibition_is_covered(prohibition: str) -> None:
    """A roll-call, so a reader can see all five are accounted for.

    Each is asserted substantively above; this exists so that removing one of
    those tests leaves a visible hole rather than a silent one.
    """
    covered = {
        "modify arbitrary database records": test_agent_reachable_code_never_executes_raw_sql,
        "bypass authorization": test_every_tool_has_a_permission_policy,
        "bypass approval": test_every_mutating_tool_declares_an_approval_action,
        "access secrets": test_no_tool_output_schema_exposes_a_credential_field,
        "execute shell commands": test_no_agent_reachable_module_executes_a_shell_command,
    }

    assert prohibition in covered
    covered[prohibition]()
