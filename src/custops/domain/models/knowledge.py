"""Knowledge documents and their embedded chunks.

Two tables rather than one: a *document* is the thing a human cites ("policy
DIS-002"), a *chunk* is the unit that gets embedded and retrieved. Collapsing
them would mean either embedding whole documents (poor retrieval, and a policy
too long to embed at all) or losing the document identity that makes a citation
meaningful.

**The vector column's width is fixed by migration.** A vector embedded at 1536
dimensions cannot be compared to one at 3072, so changing the embedding model to
a different width is a migration plus a full re-index of the corpus — not a
configuration change. ``embedding_model`` is stored per chunk so a mixed-model
corpus is detectable instead of silently returning meaningless distances.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from custops.db.base import Base, TimestampMixin

# Must match ProviderSettings.embedding_dimensions. Declared here because the
# column width is what actually enforces it.
EMBEDDING_DIMENSIONS = 1536


class KnowledgeDocument(Base, TimestampMixin):
    """A source document: a policy, a contract, a knowledge-base article.

    ``source_type`` and ``source_ref`` point back at the row this was ingested
    from, so retrieved evidence can be traced to the system of record rather
    than to a copy that may have drifted.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # One live document per source. Re-ingesting a policy updates it rather
        # than creating a second, near-identical corpus entry.
        UniqueConstraint("source_type", "source_ref", name="uq_knowledge_documents_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Scopes retrieval to one account where a document is account-specific (a
    # contract). NULL means the document applies to everyone (a policy).
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Detects a stale corpus: if the source text changed, its hash changed, and
    # re-ingestion is required. Cheaper than re-embedding to find out.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"KnowledgeDocument(source_ref={self.source_ref!r}, title={self.title!r})"


class KnowledgeChunk(Base, TimestampMixin):
    """One embedded span of a document."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_knowledge_chunks_document_id_chunk_index"
        ),
        CheckConstraint("end_offset > start_offset", name="offsets_ordered"),
        # The vector index itself is created in the migration: pgvector index
        # types take parameters SQLAlchemy cannot express portably.
        Index("ix_knowledge_chunks_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Exact span in the source document, so a citation can quote the text that
    # supports a claim instead of paraphrasing it.
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Which model produced this vector. Distances between vectors from
    # different models are meaningless; recording it makes that detectable.
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"KnowledgeChunk(document_id={self.document_id!r}, index={self.chunk_index!r})"
