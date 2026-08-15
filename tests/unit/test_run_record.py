"""What a run writes down.

The runner's persistence helpers are pure functions over state, so what ends up
in ``workflow_executions.final_state`` — and what deliberately does not — is
verifiable without a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from custops.agents.state import WorkflowState, WorkflowStatus, initial_state
from custops.apps.orchestrator.runner import (
    _coerce,
    _interrupt_from,
    _resolve_status,
    _serialisable,
    _summary,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _state(**overrides: Any) -> WorkflowState:
    state = initial_state(
        execution_id=uuid.uuid4(),
        request_id="req-1",
        raw_request="Upgrade Acme to Enterprise.",
        started_at=NOW,
    )
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


class TestSummary:
    def test_evidence_is_stored_as_citations_not_content(self) -> None:
        """The text lives in the systems of record; copying it in would drift."""
        summary = _summary(
            _state(
                evidence=[
                    {
                        "source_ref": "policy:UPG-001#chars=0-40",
                        "content": "An account is eligible when...",
                    },
                    {"source_ref": "subscription:abc", "content": "plan professional"},
                ]
            )
        )

        assert summary["evidence_citations"] == [
            "policy:UPG-001#chars=0-40",
            "subscription:abc",
        ]
        assert "An account is eligible" not in str(summary)

    def test_evidence_without_a_reference_is_dropped(self) -> None:
        """An uncitable item is not evidence a reviewer can check."""
        summary = _summary(_state(evidence=[{"content": "something unsourced"}]))

        assert summary["evidence_citations"] == []

    def test_decisions_carry_conclusions_not_reasoning(self) -> None:
        """Rule 18: the summary is a conclusion, and no other free text is kept."""
        decision = {
            "name": "upgrade_eligibility",
            "outcome": "eligible",
            "confidence": 1.0,
            "rationale_summary": "All deterministic checks passed.",
            "evidence_refs": ["contract:CTR-1"],
            "decided_at": NOW.isoformat(),
        }

        summary = _summary(_state(decisions=[decision]))

        assert summary["decisions"] == [decision]
        assert set(summary["decisions"][0]) == set(decision)

    def test_the_summary_shape_is_fixed(self) -> None:
        """A new state key must not silently start being persisted."""
        summary = _summary(_state())

        assert set(summary) == {
            "workflow_type",
            "decisions",
            "evidence_citations",
            "validation_results",
            "execution_results",
            "errors",
            "approval_status",
            "metadata",
        }

    def test_raw_request_is_not_duplicated_into_the_summary(self) -> None:
        """It already has its own column."""
        summary = _summary(_state())

        assert "raw_request" not in summary


class TestStatusResolution:
    def test_a_paused_run_reports_awaiting_approval(self) -> None:
        """The graph is the authority on whether the run stopped, not a node."""
        state = _state(status=WorkflowStatus.EXECUTING)

        assert _resolve_status(state, paused=True) == WorkflowStatus.AWAITING_APPROVAL

    def test_an_unpaused_run_keeps_its_own_status(self) -> None:
        state = _state(status=WorkflowStatus.COMPLETED)

        assert _resolve_status(state, paused=False) == WorkflowStatus.COMPLETED

    def test_a_run_with_no_status_is_failed_not_completed(self) -> None:
        """Absence of a status is not success."""
        state = _state()
        state.pop("status", None)

        assert _resolve_status(state, paused=False) == WorkflowStatus.FAILED


class TestInterruptExtraction:
    def test_a_pending_interrupt_payload_is_found(self) -> None:
        class _Interrupt:
            value: ClassVar[dict[str, str]] = {
                "approval_id": "a-1",
                "action": "subscription_upgrade",
            }

        class _Snapshot:
            interrupts = (_Interrupt(),)

        assert _interrupt_from(_Snapshot()) == {
            "approval_id": "a-1",
            "action": "subscription_upgrade",
        }

    def test_no_interrupts_means_not_paused(self) -> None:
        class _Snapshot:
            interrupts = ()

        assert _interrupt_from(_Snapshot()) is None

    def test_a_snapshot_without_the_attribute_is_handled(self) -> None:
        class _Snapshot:
            pass

        assert _interrupt_from(_Snapshot()) is None


class TestSerialisation:
    def test_uuids_and_datetimes_become_json_scalars(self) -> None:
        """Otherwise the JSONB insert fails at commit, far from its cause."""
        identifier = uuid.uuid4()

        result = _serialisable({"account_id": identifier, "at": NOW})

        assert result["account_id"] == str(identifier)
        assert result["at"] == NOW.isoformat()

    def test_nested_structures_are_coerced(self) -> None:
        identifier = uuid.uuid4()

        result = _coerce({"items": [{"id": identifier}], "count": 2})

        assert result == {"items": [{"id": str(identifier)}], "count": 2}

    def test_a_non_dict_update_is_still_recorded(self) -> None:
        """A step that returned something unexpected is still worth a row."""
        assert _serialisable("unexpected") == {"value": "unexpected"}
