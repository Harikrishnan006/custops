"""Ingestion and retrieval against a real database with pgvector.

Uses the deterministic embedder, so these assert the *pipeline* — chunking,
storage, the vector column, index usage, ordering, account scoping and Evidence
assembly — without depending on a model's semantics or an API key. Retrieval
quality is a Phase 11 evaluation question; retrieval correctness is this file's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from custops.db.engine import Database
from custops.domain.models.knowledge import EMBEDDING_DIMENSIONS, KnowledgeChunk, KnowledgeDocument
from custops.domain.seed import seed_all, seed_id
from custops.knowledge.ingestion.pipeline import (
    ingest_contracts,
    ingest_document,
    ingest_policies,
)
from custops.knowledge.retrieval.evidence import EvidenceSource
from custops.knowledge.retrieval.search import retrieve_evidence, search_chunks
from custops.providers.deterministic import DeterministicEmbeddingProvider
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

NOW = datetime.now(UTC)
PROVIDER = DeterministicEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as db_session:
        await seed_all(db_session, now=NOW)
        try:
            yield db_session
        finally:
            await db_session.rollback()


class TestIngestion:
    async def test_policies_are_chunked_and_embedded(self, session: AsyncSession) -> None:
        result = await ingest_policies(session, PROVIDER, now=NOW)

        assert result.documents_processed > 0
        assert result.chunks_written > 0

        chunk_count = (
            await session.execute(select(func.count()).select_from(KnowledgeChunk))
        ).scalar_one()
        assert chunk_count == result.chunks_written

    async def test_re_ingesting_unchanged_content_is_a_no_op(self, session: AsyncSession) -> None:
        """Otherwise every run grows the corpus and retrieval returns duplicates."""
        first = await ingest_policies(session, PROVIDER, now=NOW)
        second = await ingest_policies(session, PROVIDER, now=NOW)

        assert first.chunks_written > 0
        assert second.chunks_written == 0
        assert second.documents_unchanged == second.documents_processed

    async def test_changed_content_replaces_chunks_rather_than_appending(
        self, session: AsyncSession
    ) -> None:
        # Long enough to chunk several times over: replacement is only
        # observable if the first ingest produced more chunks than the second,
        # and `DEFAULT_MAX_CHARS` is 1200.
        document, _ = await ingest_document(
            session,
            PROVIDER,
            source_type=EvidenceSource.POLICY,
            source_ref="TEST-001",
            title="Test policy",
            body="Original text about refunds. " * 200,
            now=NOW,
        )
        # Scoped to this document, not the whole table — a global count also
        # sees seeded corpus and anything a neighbouring test ingested.
        chunks_for_document = (
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document.id)
        )
        before = (await session.execute(chunks_for_document)).scalar_one()
        assert before > 1, "fixture no longer spans multiple chunks"

        await ingest_document(
            session,
            PROVIDER,
            source_type=EvidenceSource.POLICY,
            source_ref="TEST-001",
            title="Test policy",
            body="Completely different text about upgrades.",
            now=NOW,
        )

        documents = (
            await session.execute(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.source_ref == "TEST-001")
            )
        ).scalar_one()
        after = (await session.execute(chunks_for_document)).scalar_one()

        assert documents == 1
        assert after < before  # old chunks gone, not accumulated

    async def test_chunk_offsets_point_into_the_stored_body(self, session: AsyncSession) -> None:
        """The property that makes a citation checkable."""
        document, _ = await ingest_document(
            session,
            PROVIDER,
            source_type=EvidenceSource.POLICY,
            source_ref="OFFSET-001",
            title="Offsets",
            body="First clause about upgrades. " * 60,
            now=NOW,
        )

        chunks = list(
            (
                await session.execute(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.document_id == document.id)
                    .order_by(KnowledgeChunk.chunk_index)
                )
            ).scalars()
        )

        assert chunks
        for chunk in chunks:
            assert document.body[chunk.start_offset : chunk.end_offset] == chunk.content

    async def test_contracts_are_scoped_to_their_account(self, session: AsyncSession) -> None:
        await ingest_contracts(session, PROVIDER, now=NOW)

        documents = list(
            (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.source_type == EvidenceSource.CONTRACT
                    )
                )
            ).scalars()
        )

        assert documents
        assert all(document.account_id is not None for document in documents)

    async def test_embedding_model_is_recorded_on_every_chunk(self, session: AsyncSession) -> None:
        """Distances between vectors from different models are meaningless."""
        await ingest_policies(session, PROVIDER, now=NOW)

        models = set(
            (await session.execute(select(KnowledgeChunk.embedding_model).distinct())).scalars()
        )

        assert models == {PROVIDER.model}


class TestRetrieval:
    async def test_exact_text_retrieves_its_own_chunk_first(self, session: AsyncSession) -> None:
        """A deterministic embedder makes this an exact-match assertion."""
        await ingest_policies(session, PROVIDER, now=NOW)

        target = (
            await session.execute(select(KnowledgeChunk).order_by(KnowledgeChunk.id).limit(1))
        ).scalar_one()
        embedded = await PROVIDER.embed([target.content])

        hits = await search_chunks(session, list(embedded.vectors[0]), limit=3)

        assert hits[0].chunk.id == target.id
        assert hits[0].similarity == pytest.approx(1.0, abs=1e-4)

    async def test_results_are_ordered_by_similarity(self, session: AsyncSession) -> None:
        await ingest_policies(session, PROVIDER, now=NOW)
        embedded = await PROVIDER.embed(["discount approval thresholds"])

        hits = await search_chunks(session, list(embedded.vectors[0]), limit=5)

        similarities = [hit.similarity for hit in hits]
        assert similarities == sorted(similarities, reverse=True)

    async def test_without_account_context_only_global_documents_are_returned(
        self, session: AsyncSession
    ) -> None:
        """Never fall back to 'all documents' — that returns other customers' contracts."""
        await ingest_policies(session, PROVIDER, now=NOW)
        await ingest_contracts(session, PROVIDER, now=NOW)
        embedded = await PROVIDER.embed(["plan changes"])

        hits = await search_chunks(session, list(embedded.vectors[0]), limit=20)

        assert hits
        assert all(hit.document.account_id is None for hit in hits)

    async def test_account_scoping_excludes_other_accounts_contracts(
        self, session: AsyncSession
    ) -> None:
        await ingest_contracts(session, PROVIDER, now=NOW)
        acme = seed_id("account", "acme")
        embedded = await PROVIDER.embed(["plan changes"])

        hits = await search_chunks(session, list(embedded.vectors[0]), limit=20, account_id=acme)

        contract_accounts = {
            hit.document.account_id
            for hit in hits
            if hit.document.source_type == EvidenceSource.CONTRACT
        }
        assert contract_accounts <= {acme}

    async def test_unknown_account_sees_only_global_documents(self, session: AsyncSession) -> None:
        await ingest_policies(session, PROVIDER, now=NOW)
        await ingest_contracts(session, PROVIDER, now=NOW)
        embedded = await PROVIDER.embed(["plan changes"])

        hits = await search_chunks(
            session, list(embedded.vectors[0]), limit=20, account_id=uuid.uuid4()
        )

        assert all(hit.document.account_id is None for hit in hits)

    async def test_source_type_filter(self, session: AsyncSession) -> None:
        await ingest_policies(session, PROVIDER, now=NOW)
        embedded = await PROVIDER.embed(["refunds"])

        hits = await search_chunks(
            session,
            list(embedded.vectors[0]),
            limit=10,
            source_types=(EvidenceSource.POLICY,),
        )

        assert all(hit.document.source_type == EvidenceSource.POLICY for hit in hits)


class TestEvidenceAssembly:
    async def test_evidence_carries_checkable_citations(self, session: AsyncSession) -> None:
        await ingest_policies(session, PROVIDER, now=NOW)

        evidence = await retrieve_evidence(
            session, PROVIDER, "Discounts above 20% require approval.", now=NOW
        )

        assert len(evidence) > 0
        for citation in evidence.citations:
            assert "#chars=" in citation

    async def test_exact_policy_text_is_judged_sufficient(self, session: AsyncSession) -> None:
        await ingest_policies(session, PROVIDER, now=NOW)
        target = (
            await session.execute(select(KnowledgeChunk).order_by(KnowledgeChunk.id).limit(1))
        ).scalar_one()

        evidence = await retrieve_evidence(session, PROVIDER, target.content, now=NOW)

        assert evidence.sufficient
        assert evidence.confidence > 0.9

    async def test_unrelated_query_is_judged_insufficient(self, session: AsyncSession) -> None:
        """The escalate branch (§7) must actually fire on weak retrieval."""
        await ingest_policies(session, PROVIDER, now=NOW)

        evidence = await retrieve_evidence(
            session, PROVIDER, "zzzz unrelated gibberish query", now=NOW
        )

        assert not evidence.sufficient
        assert "below the" in evidence.reason or "No matching" in evidence.reason

    async def test_audit_payload_is_persistable(self, session: AsyncSession) -> None:
        await ingest_policies(session, PROVIDER, now=NOW)

        evidence = await retrieve_evidence(session, PROVIDER, "refund authority", now=NOW)
        payload = evidence.to_audit_payload()

        assert payload["query"] == "refund authority"
        assert isinstance(payload["citations"], list)
