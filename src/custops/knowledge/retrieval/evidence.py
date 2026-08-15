"""The Evidence model.

BUILD_SPEC §6: the Research agent returns *structured evidence with source
references, never prose*. This module is that contract.

Every item carries where it came from, precisely enough to go and check: a
source reference, the character span within the source, and the retrieval score
that surfaced it. An approval request built from these can show a human the
sentence a decision rests on, rather than a summary of it — which is the
difference between an audit trail and a story about one.

``Evidence`` is deliberately serialisable and free of model output: no
chain-of-thought, no narration (Rule 18). A model may later *interpret* these
items; it does not get to author them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EvidenceSource(StrEnum):
    """Where a piece of evidence came from.

    Retrieved-from-corpus and read-from-system-of-record are both evidence, but
    they carry different weight: a policy chunk is an interpretation aid, while
    a subscription row is a fact. Distinguishing them keeps a retrieved
    paraphrase from being mistaken for state.
    """

    POLICY = "policy"
    CONTRACT = "contract"
    KNOWLEDGE = "knowledge"
    SUBSCRIPTION = "subscription"
    INVOICE = "invoice"
    ACCOUNT = "account"
    SUPPORT = "support"
    ENTITLEMENT = "entitlement"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One retrievable, checkable fact."""

    source: str
    # Stable pointer to the origin, e.g. "policy:DIS-002" or
    # "subscription:5f3e...". Quotable in an approval request.
    source_ref: str
    content: str

    # Character span within the source document, when the item came from a
    # chunked document. Absent for system-of-record reads, which are whole rows.
    start_offset: int | None = None
    end_offset: int | None = None

    # Retrieval score, when the item was retrieved rather than read directly.
    # None for a system-of-record read: those are not ranked, they are facts.
    similarity: float | None = None

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        """A human-quotable pointer at the exact span."""
        if self.start_offset is None or self.end_offset is None:
            return self.source_ref
        return f"{self.source_ref}#chars={self.start_offset}-{self.end_offset}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything gathered for one question, with its confidence.

    ``sufficient`` drives the graph's escalate-vs-decide branch (§7) and is
    computed by a deterministic rule over similarity scores — see
    ``domain.policies.retrieval``.
    """

    query: str
    items: tuple[EvidenceItem, ...]
    retrieved_at: datetime
    sufficient: bool
    confidence: float
    reason: str

    def __len__(self) -> int:
        return len(self.items)

    def by_source(self, source: str) -> tuple[EvidenceItem, ...]:
        return tuple(item for item in self.items if item.source == source)

    @property
    def citations(self) -> tuple[str, ...]:
        """Every source reference, for attaching to an audit event."""
        return tuple(item.citation for item in self.items)

    def to_audit_payload(self) -> dict[str, Any]:
        """A compact, structured form safe to persist in ``audit_events``.

        Citations and scores, not content: the full text already lives in the
        system of record, and copying it into the audit log would duplicate
        data that can drift and inflate rows that are read on every trace.
        """
        return {
            "query": self.query,
            "sufficient": self.sufficient,
            "confidence": self.confidence,
            "reason": self.reason,
            "item_count": len(self.items),
            "citations": list(self.citations),
        }
