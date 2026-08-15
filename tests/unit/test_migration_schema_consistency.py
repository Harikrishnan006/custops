"""Do the migrations actually build the schema the models declare?

Hand-written migrations drift from models silently, and the usual safety net —
``alembic check`` or ``--autogenerate`` — needs a live database. This test closes
that gap without one: it renders the full migration chain as SQL in Alembic's
offline mode, parses the resulting DDL, and compares it column-by-column against
``Base.metadata``.

It catches exactly the mistakes hand-writing invites: a column added to a model
and forgotten in the migration, a name typo, a table that never got created.

It does *not* replace ``alembic check`` against a real server, which additionally
verifies types, server defaults and constraints. That remains a completion task
for when PostgreSQL is available.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import custops.domain.models  # noqa: F401  (populates Base.metadata)
from custops.db.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Alembic's own bookkeeping table is not part of our metadata.
IGNORED_TABLES = frozenset({"alembic_version"})

_CREATE_TABLE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\);", re.DOTALL)
_CONSTRAINT_KEYWORDS = ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")


@pytest.fixture(scope="module")
def rendered_ddl() -> str:
    """Render every migration as SQL, with no database involved."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stderr}"
    return result.stdout


@pytest.fixture(scope="module")
def ddl_tables(rendered_ddl: str) -> dict[str, set[str]]:
    """Map each created table to its column names, parsed from the DDL."""
    tables: dict[str, set[str]] = {}
    for table_name, body in _CREATE_TABLE.findall(rendered_ddl):
        if table_name in IGNORED_TABLES:
            continue
        columns: set[str] = set()
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",").strip()
            if not line or line.startswith(_CONSTRAINT_KEYWORDS):
                continue
            columns.add(line.split()[0])
        tables[table_name] = columns
    return tables


def test_every_model_table_is_created_by_a_migration(ddl_tables: dict[str, set[str]]) -> None:
    missing = set(Base.metadata.tables) - set(ddl_tables)

    assert not missing, f"declared in models but never created by a migration: {sorted(missing)}"


def test_no_migration_creates_a_table_the_models_do_not_declare(
    ddl_tables: dict[str, set[str]],
) -> None:
    orphaned = set(ddl_tables) - set(Base.metadata.tables)

    assert not orphaned, f"created by a migration but absent from the models: {sorted(orphaned)}"


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
def test_columns_match_between_model_and_migration(
    table_name: str, ddl_tables: dict[str, set[str]]
) -> None:
    model_columns = {column.name for column in Base.metadata.tables[table_name].columns}
    migration_columns = ddl_tables[table_name]

    assert model_columns == migration_columns, (
        f"{table_name}: "
        f"missing from migration={sorted(model_columns - migration_columns)}, "
        f"absent from model={sorted(migration_columns - model_columns)}"
    )


def test_revision_history_is_linear() -> None:
    """Multiple heads silently apply only part of the schema."""
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert len(script.get_heads()) == 1, f"branched migration history: {script.get_heads()}"


def test_every_revision_is_reachable_from_head() -> None:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    head = script.get_current_head()
    assert head is not None

    reachable = {revision.revision for revision in script.walk_revisions("base", head)}
    all_revisions = {revision.revision for revision in script.walk_revisions()}

    assert reachable == all_revisions
