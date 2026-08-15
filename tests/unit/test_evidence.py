"""Evidence and the retrieval-sufficiency rule.

Sufficiency drives the graph's escalate-vs-decide branch (§7). It is a
deterministic function of similarity scores precisely because "I don't have
enough information" is the judgement least likely to survive being delegated to
an eager model.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custops.domain.policies.retrieval import RetrievalPolicy, assess_sufficiency
from custops.knowledge.retrieval.evidence import Evidence, EvidenceItem, EvidenceSource

NOW = datetime(2026, 8, 15, tzinfo=UTC)


class TestSufficiency:
    def test_no_results_is_insufficient(self) -> None:
        assessment = assess_sufficiency([])

        assert not assessment.sufficient
        assert assessment.confidence == 0.0
        assert "No matching evidence" in assessment.reason

    def test_everything_below_the_floor_is_insufficient(self) -> None:
        assessment = assess_sufficiency([0.20, 0.15, 0.05])

        assert not assessment.sufficient
        assert "below the" in assessment.reason

    def test_a_strong_single_match_is_sufficient(self) -> None:
        """One exact policy match beats five vague ones."""
        assessment = assess_sufficiency([0.91])

        assert assessment.sufficient
        assert assessment.confidence == pytest.approx(0.91)

    def test_several_usable_matches_are_sufficient(self) -> None:
        assessment = assess_sufficiency([0.55, 0.48, 0.40])

        assert assessment.sufficient

    def test_confidence_reflects_the_best_match(self) -> None:
        assessment = assess_sufficiency([0.42, 0.83, 0.10])

        assert assessment.confidence == pytest.approx(0.83)

    def test_thresholds_are_configurable(self) -> None:
        strict = RetrievalPolicy(minimum_similarity=0.9, strong_match_similarity=0.99)

        assert not assess_sufficiency([0.85], policy=strict).sufficient
        assert assess_sufficiency([0.85]).sufficient

    def test_minimum_results_is_enforced(self) -> None:
        policy = RetrievalPolicy(minimum_similarity=0.3, minimum_results=3)

        assert not assess_sufficiency([0.5, 0.4], policy=policy).sufficient
        assert assess_sufficiency([0.5, 0.4, 0.35], policy=policy).sufficient

    def test_negative_similarity_does_not_produce_negative_confidence(self) -> None:
        assessment = assess_sufficiency([-0.4])

        assert assessment.confidence == 0.0

    def test_result_is_reproducible(self) -> None:
        scores = [0.61, 0.44]

        assert assess_sufficiency(scores) == assess_sufficiency(scores)


class TestEvidenceItem:
    def test_citation_includes_the_character_span(self) -> None:
        item = EvidenceItem(
            source=EvidenceSource.POLICY,
            source_ref="policy:DIS-002",
            content="Discounts above 20% require approval.",
            start_offset=340,
            end_offset=612,
            similarity=0.82,
        )

        assert item.citation == "policy:DIS-002#chars=340-612"

    def test_citation_without_offsets_is_the_bare_reference(self) -> None:
        """A system-of-record read is a whole row, not a span."""
        item = EvidenceItem(
            source=EvidenceSource.SUBSCRIPTION,
            source_ref="subscription:5f3e",
            content="plan=professional",
        )

        assert item.citation == "subscription:5f3e"
        assert item.similarity is None


class TestEvidence:
    def _evidence(self) -> Evidence:
        return Evidence(
            query="Can Acme upgrade mid-term?",
            items=(
                EvidenceItem(
                    source=EvidenceSource.POLICY,
                    source_ref="policy:UPG-001",
                    content="An account is eligible when...",
                    start_offset=0,
                    end_offset=30,
                    similarity=0.88,
                ),
                EvidenceItem(
                    source=EvidenceSource.CONTRACT,
                    source_ref="CTR-ACME-001",
                    content="Section 4.2 - Plan Changes...",
                    start_offset=10,
                    end_offset=44,
                    similarity=0.71,
                ),
            ),
            retrieved_at=NOW,
            sufficient=True,
            confidence=0.88,
            reason="Strong match.",
        )

    def test_filtering_by_source(self) -> None:
        evidence = self._evidence()

        assert len(evidence.by_source(EvidenceSource.POLICY)) == 1
        assert len(evidence.by_source(EvidenceSource.INVOICE)) == 0

    def test_length_is_the_item_count(self) -> None:
        assert len(self._evidence()) == 2

    def test_audit_payload_carries_citations_not_content(self) -> None:
        """The full text already lives in the system of record; copying it into
        the audit log duplicates data that can drift."""
        payload = self._evidence().to_audit_payload()

        assert payload["citations"] == [
            "policy:UPG-001#chars=0-30",
            "CTR-ACME-001#chars=10-44",
        ]
        assert payload["item_count"] == 2
        assert payload["confidence"] == 0.88
        assert "An account is eligible" not in str(payload)

    def test_audit_payload_contains_no_narration(self) -> None:
        """Rule 18: structured references and scores, never chain-of-thought."""
        payload = self._evidence().to_audit_payload()

        assert set(payload) == {
            "query",
            "sufficient",
            "confidence",
            "reason",
            "item_count",
            "citations",
        }
