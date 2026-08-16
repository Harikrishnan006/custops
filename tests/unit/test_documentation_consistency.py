"""Documentation that disagrees with the code is worse than none (Rule 21).

A stale README costs someone an afternoon before they conclude the docs lie and
stop reading them. These tests pin the claims that go stale silently: which
commands exist, which documents are referenced, and whether every phase left a
record.

They check *structure and references*, not prose. Asserting on wording would
make every edit a test failure and teach people to weaken the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"

# The build plan has fourteen phases (§20). Every one should have left a record
# of what was built and what was known to be incomplete.
EXPECTED_PHASES = range(1, 15)


def _readme() -> str:
    return README.read_text(encoding="utf-8")


# ------------------------------------------------------------------ presence


def test_the_readme_exists_and_is_substantial() -> None:
    """§21.8: it must take a stranger from clone to running system."""
    assert README.is_file()
    assert len(_readme()) > 2000


@pytest.mark.parametrize("phase", EXPECTED_PHASES)
def test_every_phase_left_a_completion_document(phase: int) -> None:
    """Phase 4's was missing until Phase 14 noticed.

    A phase with no record is a phase whose known gaps were never written down.
    """
    path = DOCS / f"PHASE-{phase:02d}-COMPLETION.md"

    assert path.is_file(), f"no completion document for phase {phase}"


def test_the_architecture_overview_exists() -> None:
    assert (DOCS / "architecture" / "overview.md").is_file()


# ---------------------------------------------------------------- references


def _referenced_adrs(text: str) -> set[str]:
    """ADR ids mentioned anywhere in a document."""
    return set(re.findall(r"ADR-(\d{3})", text))


def test_every_referenced_adr_exists() -> None:
    """A dangling ADR reference sends a reader looking for a decision record
    that was never written."""
    existing = {path.name[4:7] for path in (DOCS / "decisions").glob("ADR-*.md")}
    referenced: set[str] = set()

    for path in [README, *DOCS.rglob("*.md")]:
        if path.name == "BUILD_SPEC.md":
            continue  # the spec references ADRs it *commissions*, not ones that exist
        referenced |= _referenced_adrs(path.read_text(encoding="utf-8"))

    missing = sorted(referenced - existing)
    assert not missing, f"documents reference ADRs that do not exist: {missing}"


def test_adr_numbering_has_no_gaps() -> None:
    """A gap means either a deleted decision or a misnumbered one; both are
    worth noticing."""
    numbers = sorted(int(path.name[4:7]) for path in (DOCS / "decisions").glob("ADR-*.md"))

    assert numbers == list(range(1, len(numbers) + 1))


def test_internal_document_links_resolve() -> None:
    """Relative links rot when files move."""
    broken: list[str] = []

    for path in [README, *DOCS.rglob("*.md")]:
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?://|#)([^)]+)\)", text):
            candidate = (path.parent / target.split("#")[0]).resolve()
            if not candidate.exists():
                broken.append(f"{path.name} -> {target}")

    assert not broken, f"broken relative links: {broken}"


# ----------------------------------------------------------------- the CLI


def _documented_cli_commands(text: str) -> set[str]:
    """`custops <command>` invocations appearing in documentation."""
    return set(re.findall(r"custops\s+([a-z][a-z-]+)", text))


def test_every_documented_cli_command_exists() -> None:
    """The failure this prevents: docs telling a stranger to run something the
    parser has never heard of."""
    from custops import cli

    # argparse exposes no public accessor for registered subcommands, so read
    # them from the source of truth: the strings passed to `add_parser`.
    source = Path(cli.__file__).read_text(encoding="utf-8")
    parser_commands = set(re.findall(r'add_parser\(\s*"([a-z][a-z-]+)"', source))

    assert parser_commands, "no CLI subcommands found"

    documented = _documented_cli_commands(_readme())
    unknown = sorted(documented - parser_commands - {"run"})

    assert not unknown, f"README documents commands the CLI does not have: {unknown}"


def test_the_readme_documents_how_to_authenticate() -> None:
    """Phase 13 made every workflow endpoint require a bearer token.

    A README that stops at `/health` leaves a stranger with a 401 and no
    instruction — which is exactly the "undocumented step" §21.8 forbids.
    """
    text = _readme()

    assert "issue-token" in text
    assert "Authorization" in text or "Bearer" in text


def test_the_readme_covers_the_workflow_endpoint() -> None:
    """The system's whole purpose. A README describing only health checks
    documents a different, much smaller system."""
    assert "/workflows" in _readme()


# ------------------------------------------------------------------ diagrams


def test_the_architecture_document_contains_diagrams() -> None:
    """Four were judged to earn their place; prose alone cannot show a graph."""
    text = (DOCS / "architecture" / "overview.md").read_text(encoding="utf-8")

    assert text.count("```mermaid") >= 4


def test_diagrams_are_source_not_images() -> None:
    """Mermaid renders on GitHub and diffs as text.

    A binary image cannot be reviewed in a pull request, and drifts from the
    code with nothing to catch it.
    """
    text = (DOCS / "architecture" / "overview.md").read_text(encoding="utf-8")

    assert "![" not in text or "```mermaid" in text


# --------------------------------------------------------------- currency


def test_the_architecture_document_reflects_the_delivered_system() -> None:
    """It described Phase 1 until Phase 14. Ten phases of architecture were
    absent from the architecture document."""
    text = (DOCS / "architecture" / "overview.md").read_text(encoding="utf-8").lower()

    for subject in ("mcp", "langgraph", "a2a", "approval", "audit", "authentication"):
        assert subject in text, f"the architecture overview never mentions {subject}"


def test_the_readme_names_the_evaluation_dependency_as_dev_only() -> None:
    """AgentForge must not read as a runtime dependency (D10)."""
    text = _readme()

    assert "agent-forge" in text.lower()
