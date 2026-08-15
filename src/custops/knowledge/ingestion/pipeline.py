"""Ingestion: source rows to embedded, retrievable chunks.

Ingestion is **idempotent and content-addressed**. Each document records a hash
of its body; re-ingesting unchanged text is a no-op, and changed text replaces
that document's chunks wholesale rather than appending a second copy. Without
this, every ingestion run would grow the corpus and retrieval would start
returning three near-identical versions of the same policy clause.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.domain.models.contract import Contract, Policy
from custops.domain.models.knowledge import KnowledgeChunk, KnowledgeDocument
from custops.knowledge.ingestion.chunking import chunk_text
from custops.knowledge.retrieval.evidence import EvidenceSource
from custops.observability.logging import get_logger
from custops.providers.base import EmbeddingProvider

logger = get_logger(__name__)

# Embedding APIs are billed and rate-limited per request; batching keeps a
# large document to a handful of calls instead of one per chunk.
EMBED_BATCH_SIZE = 64


@dataclass(frozen=True, slots=True)
class IngestionResult:
    documents_processed: int
    documents_updated: int
    documents_unchanged: int
    chunks_written: int


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ingest_document(
    session: AsyncSession,
    provider: EmbeddingProvider,
    *,
    source_type: str,
    source_ref: str,
    title: str,
    body: str,
    category: str | None = None,
    account_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[KnowledgeDocument, int]:
    """Ingest one document, returning it and the number of chunks written.

    Zero chunks written means the content was unchanged and nothing needed
    re-embedding — the common case on a repeat run.
    """
    timestamp = now if now is not None else datetime.now(UTC)
    digest = content_hash(body)

    existing = (
        await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source_type == source_type,
                KnowledgeDocument.source_ref == source_ref,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.content_hash == digest:
        return existing, 0

    if existing is None:
        document = KnowledgeDocument(
            id=uuid.uuid4(),
            source_type=source_type,
            source_ref=source_ref,
            title=title,
            category=category,
            body=body,
            account_id=account_id,
            content_hash=digest,
            ingested_at=timestamp,
            metadata_={},
        )
        session.add(document)
        await session.flush()
    else:
        document = existing
        document.title = title
        document.category = category
        document.body = body
        document.account_id = account_id
        document.content_hash = digest
        document.ingested_at = timestamp
        # Replace, never append: stale chunks of the previous text would keep
        # being retrieved and cited as current.
        await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
        )

    chunks = chunk_text(body)
    if not chunks:
        return document, 0

    written = 0
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        embedded = await provider.embed([chunk.text for chunk in batch])

        if len(embedded.vectors) != len(batch):
            raise RuntimeError(
                f"Embedding provider returned {len(embedded.vectors)} vectors "
                f"for {len(batch)} chunks; refusing to store misaligned data."
            )

        for chunk, vector in zip(batch, embedded.vectors, strict=True):
            session.add(
                KnowledgeChunk(
                    id=uuid.uuid4(),
                    document_id=document.id,
                    chunk_index=chunk.index,
                    content=chunk.text,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    embedding=list(vector),
                    embedding_model=embedded.model,
                )
            )
            written += 1

    await session.flush()
    return document, written


async def ingest_policies(
    session: AsyncSession,
    provider: EmbeddingProvider,
    *,
    now: datetime | None = None,
) -> IngestionResult:
    """Ingest every policy. Policies are global — no account scoping."""
    policies = list((await session.execute(select(Policy))).scalars())
    return await _ingest_many(
        session,
        provider,
        [
            {
                "source_type": EvidenceSource.POLICY,
                "source_ref": f"{policy.code}:v{policy.version}",
                "title": policy.title,
                "body": policy.body,
                "category": policy.category,
                "account_id": None,
            }
            for policy in policies
        ],
        now=now,
    )


async def ingest_contracts(
    session: AsyncSession,
    provider: EmbeddingProvider,
    *,
    now: datetime | None = None,
) -> IngestionResult:
    """Ingest contract prose, scoped to the owning account.

    Only contracts with a document body: a contract row without prose carries
    its meaning entirely in structured fields, which the deterministic rules
    already read directly.
    """
    contracts = list(
        (
            await session.execute(select(Contract).where(Contract.document_body.is_not(None)))
        ).scalars()
    )
    return await _ingest_many(
        session,
        provider,
        [
            {
                "source_type": EvidenceSource.CONTRACT,
                "source_ref": contract.contract_number,
                "title": f"Contract {contract.contract_number}",
                "body": contract.document_body or "",
                "category": "contract",
                "account_id": contract.account_id,
            }
            for contract in contracts
        ],
        now=now,
    )


async def _ingest_many(
    session: AsyncSession,
    provider: EmbeddingProvider,
    documents: list[dict[str, object]],
    *,
    now: datetime | None,
) -> IngestionResult:
    updated = 0
    unchanged = 0
    chunks_written = 0

    for spec in documents:
        _, written = await ingest_document(
            session,
            provider,
            source_type=str(spec["source_type"]),
            source_ref=str(spec["source_ref"]),
            title=str(spec["title"]),
            body=str(spec["body"]),
            category=spec["category"] if isinstance(spec["category"], str) else None,
            account_id=spec["account_id"] if isinstance(spec["account_id"], uuid.UUID) else None,
            now=now,
        )
        chunks_written += written
        if written:
            updated += 1
        else:
            unchanged += 1

    logger.info(
        "ingestion_completed",
        documents=len(documents),
        updated=updated,
        unchanged=unchanged,
        chunks=chunks_written,
    )
    return IngestionResult(
        documents_processed=len(documents),
        documents_updated=updated,
        documents_unchanged=unchanged,
        chunks_written=chunks_written,
    )
