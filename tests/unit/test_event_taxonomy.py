"""Every §16 event type must actually be emitted somewhere.

This is the test that would have caught the state Phase 12 found: nineteen event
types defined since Phase 1, **four** ever written, and fifteen enum members that
existed only as documentation. A taxonomy nobody emits is worse than no taxonomy
— it makes a trace look complete when it is missing three quarters of what it
claims to record.

The check is a source scan rather than a runtime assertion, deliberately.
Proving an event fires at runtime needs a database and a full workflow; proving
it is *wired* needs neither, so this runs everywhere and fails fast. It is a
structural guard, not a behavioural one — the integration tests cover firing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from custops.observability.events import WORKFLOW_EVENT_NAMES, ActorType, EventType

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "custops"

# The module that *defines* the taxonomy naturally mentions every member; it
# must not count as an emission site or the test proves nothing.
DEFINITION_MODULE = SOURCE_ROOT / "observability" / "events.py"


def _source_files() -> list[Path]:
    return [path for path in SOURCE_ROOT.rglob("*.py") if path != DEFINITION_MODULE]


def _emitted_members() -> set[str]:
    """Every ``EventType.X`` referenced outside the defining module.

    Parsed with ``ast`` rather than grepped, so a member named in a comment or a
    docstring cannot masquerade as an emission site.
    """
    found: set[str] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "EventType"
            ):
                found.add(node.attr)
    return found


def test_every_event_type_has_an_emission_site() -> None:
    """Adding an EventType without emitting it must fail here.

    If this fails you have two honest options: wire the event at the point it
    occurs, or delete it from the enum. Silencing the test by adding the name to
    an exemption list would restore exactly the situation it exists to prevent.
    """
    defined = {member.name for member in EventType}
    emitted = _emitted_members()

    missing = sorted(defined - emitted)
    assert not missing, (
        f"{len(missing)} EventType member(s) are defined but never emitted: "
        f"{', '.join(missing)}. Wire them where the event occurs, or remove them."
    )


def test_no_emission_site_invents_an_event_type() -> None:
    """The reverse direction: a typo'd member would be an AttributeError at
    runtime, but only on the path that happens to run. Catch it statically."""
    defined = {member.name for member in EventType}

    invented = sorted(_emitted_members() - defined)

    assert not invented, f"referenced but not defined in EventType: {', '.join(invented)}"


def test_the_taxonomy_matches_the_specification_exactly() -> None:
    """§16 fixes the vocabulary. Drift in either direction is a spec violation."""
    specification = {
        "request_received",
        "workflow_classified",
        "plan_created",
        "retrieval_started",
        "retrieval_completed",
        "tool_selected",
        "tool_called",
        "tool_completed",
        "a2a_request_sent",
        "a2a_response_received",
        "decision_made",
        "approval_requested",
        "approval_received",
        "validation_started",
        "validation_completed",
        "retry",
        "replan",
        "workflow_completed",
        "workflow_failed",
    }

    assert {str(event) for event in EventType} == specification
    assert specification == WORKFLOW_EVENT_NAMES


def test_the_specification_defines_nineteen_events() -> None:
    """A count, so an accidental deletion is as visible as an addition."""
    assert len(EventType) == 19


# --------------------------------------------------------------- where they fire


@pytest.mark.parametrize(
    ("member", "module"),
    [
        ("REQUEST_RECEIVED", "apps/orchestrator/runner.py"),
        ("WORKFLOW_CLASSIFIED", "agents/nodes.py"),
        ("PLAN_CREATED", "agents/nodes.py"),
        ("RETRIEVAL_STARTED", "agents/nodes.py"),
        ("RETRIEVAL_COMPLETED", "agents/nodes.py"),
        ("TOOL_SELECTED", "mcp/tools/runtime.py"),
        ("TOOL_CALLED", "mcp/tools/runtime.py"),
        ("TOOL_COMPLETED", "mcp/tools/runtime.py"),
        ("A2A_REQUEST_SENT", "agents/nodes.py"),
        ("A2A_RESPONSE_RECEIVED", "agents/nodes.py"),
        ("DECISION_MADE", "agents/nodes.py"),
        ("APPROVAL_REQUESTED", "agents/nodes.py"),
        ("APPROVAL_RECEIVED", "apps/api/routers/approvals.py"),
        ("VALIDATION_STARTED", "agents/nodes.py"),
        ("VALIDATION_COMPLETED", "agents/nodes.py"),
        ("RETRY", "apps/orchestrator/runner.py"),
        ("REPLAN", "apps/orchestrator/runner.py"),
        ("WORKFLOW_COMPLETED", "apps/orchestrator/runner.py"),
        ("WORKFLOW_FAILED", "apps/orchestrator/runner.py"),
    ],
)
def test_each_event_fires_from_the_layer_that_owns_it(member: str, module: str) -> None:
    """Pins *where*, not just *that*.

    An event emitted from the wrong layer produces a trace that misattributes
    what happened — a tool event raised by the API, say, would put the API in
    the audit trail instead of the agent that called the tool.
    """
    source = (SOURCE_ROOT / module).read_text(encoding="utf-8")

    assert f"EventType.{member}" in source, f"{member} is not emitted from {module}"


# ------------------------------------------------------------------ actor types


def test_actor_types_cover_the_three_kinds_of_cause() -> None:
    """Human, agent, system. Collapsing any two makes the approval trail
    meaningless — §13 depends on telling a human decision from an agent action.
    """
    assert {str(actor) for actor in ActorType} == {"user", "agent", "system"}


def test_no_hand_built_audit_events_remain_outside_the_recorder() -> None:
    """One recording path (§16).

    Three hand-built ``AuditEvent(...)`` sites existed before Phase 12, each
    free to forget redaction, the actor, or the correlation id. Fifteen more
    would have guaranteed drift, so construction now lives in one module.
    """
    recorder = SOURCE_ROOT / "observability" / "audit.py"
    offenders: list[str] = []

    for path in _source_files():
        if path == recorder or "models" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"\bAuditEvent\s*\(", source):
            offenders.append(str(path.relative_to(SOURCE_ROOT)))

    assert not offenders, (
        "audit rows must be written through observability.audit.record_event, "
        f"but these construct AuditEvent directly: {', '.join(offenders)}"
    )
