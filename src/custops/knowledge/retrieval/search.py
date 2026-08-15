"""Vector search over the knowledge corpus."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from custops.domain.models.knowledge import KnowledgeChunk, KnowledgeDocument
from custops.domain.policies.retrieval import RetrievalPolicy, assess_sufficiency
from custops.knowledge.retrieval.evidence import Evidence, EvidenceItem
from custops.providers.base import EmbeddingProvider

DEFAULT_LIMIT = 5


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A chunk and how close it was."""

    chunk: KnowledgeChunk
    document: KnowledgeDocument
    similarity: float


async def search_chunks(
    session: AsyncSession,
    query_vector: list[float],
    *,
    limit: int = DEFAULT_LIMIT,
    account_id: uuid.UUID | None = None,
    source_types: tuple[str, ...] | None = None,
) -> list[SearchHit]:
    """Nearest chunks by cosine distance.

    ``account_id`` scoping is a correctness boundary, not a filter for
    convenience: contracts are account-specific, and retrieving one customer's
    contract while reasoning about another is a data leak with commercial
    consequences. Documents with a NULL ``account_id`` (policies) apply to
    everyone and are always in scope.
    """
    distance = KnowledgeChunk.embedding.cosine_distance(query_vector)

    statement = (
        select(KnowledgeChunk, KnowledgeDocument, distance.label("distance"))
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .options(selectinload(KnowledgeChunk.document))
        # Order by the indexed expression so the HNSW index is usable; ordering
        # by a computed similarity instead would force a sequential scan.
        .order_by(distance)
        .limit(limit)
    )

    if account_id is not None:
        statement = statement.where(
            (KnowledgeDocument.account_id.is_(None)) | (KnowledgeDocument.account_id == account_id)
        )
    else:
        # No account context: global documents only. Never fall back to "all
        # documents", which would return other customers' contracts.
        statement = statement.where(KnowledgeDocument.account_id.is_(None))

    if source_types:
        statement = statement.where(KnowledgeDocument.source_type.in_(source_types))

    rows = (await session.execute(statement)).all()
    return [
        SearchHit(
            chunk=chunk,
            document=document,
            # pgvector's cosine distance is 1 - cosine similarity.
            similarity=round(1.0 - float(raw_distance), 6),
        )
        for chunk, document, raw_distance in rows
    ]


async def retrieve_evidence(
    session: AsyncSession,
    provider: EmbeddingProvider,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    account_id: uuid.UUID | None = None,
    source_types: tuple[str, ...] | None = None,
    policy: RetrievalPolicy | None = None,
    now: datetime | None = None,
) -> Evidence:
    """Embed a question, search, and assemble structured Evidence.

    The sufficiency verdict comes from a deterministic rule over the similarity
    scores — the model never decides whether its own evidence was good enough.
    """
    embedded = await provider.embed([query])
    query_vector = list(embedded.vectors[0]) if embedded.vectors else []

    hits = await search_chunks(
        session,
        query_vector,
        limit=limit,
        account_id=account_id,
        source_types=source_types,
    )

    assessment = assess_sufficiency([hit.similarity for hit in hits], policy=policy)

    items = tuple(
        EvidenceItem(
            source=hit.document.source_type,
            source_ref=hit.document.source_ref,
            content=hit.chunk.content,
            start_offset=hit.chunk.start_offset,
            end_offset=hit.chunk.end_offset,
            similarity=hit.similarity,
            metadata={
                "title": hit.document.title,
                "category": hit.document.category,
                "chunk_index": hit.chunk.chunk_index,
                "embedding_model": hit.chunk.embedding_model,
            },
        )
        for hit in hits
    )

    return Evidence(
        query=query,
        items=items,
        retrieved_at=now if now is not None else datetime.now(UTC),
        sufficient=assessment.sufficient,
        confidence=assessment.confidence,
        reason=assessment.reason,
    )
