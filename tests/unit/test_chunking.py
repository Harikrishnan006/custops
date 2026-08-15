"""Chunking — the properties retrieval and citation depend on."""

from __future__ import annotations

from itertools import pairwise

import pytest

from custops.knowledge.ingestion.chunking import (
    Chunk,
    ChunkingError,
    chunk_text,
)

PARAGRAPHS = "\n\n".join(f"Paragraph {i}. " + ("word " * 40).strip() + "." for i in range(6))


def test_short_text_is_a_single_chunk() -> None:
    chunks = chunk_text("A short policy statement.", max_chars=1200)

    assert len(chunks) == 1
    assert chunks[0].text == "A short policy statement."
    assert chunks[0].index == 0


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t  \n"])
def test_empty_or_whitespace_yields_no_chunks(text: str) -> None:
    assert chunk_text(text) == []


def test_offsets_reproduce_the_chunk_exactly() -> None:
    """The property that makes citation trustworthy."""
    chunks = chunk_text(PARAGRAPHS, max_chars=300, overlap_chars=50)

    assert len(chunks) > 1
    for chunk in chunks:
        assert PARAGRAPHS[chunk.start_offset : chunk.end_offset] == chunk.text


def test_chunking_is_deterministic() -> None:
    first = chunk_text(PARAGRAPHS, max_chars=250, overlap_chars=40)
    second = chunk_text(PARAGRAPHS, max_chars=250, overlap_chars=40)

    assert first == second


def test_indexes_are_sequential_from_zero() -> None:
    chunks = chunk_text(PARAGRAPHS, max_chars=250, overlap_chars=40)

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_chunks_respect_the_size_limit() -> None:
    max_chars = 200
    chunks = chunk_text(PARAGRAPHS, max_chars=max_chars, overlap_chars=30)

    assert all(len(chunk.text) <= max_chars for chunk in chunks)


def test_whole_document_is_covered() -> None:
    """No content may be silently dropped between chunks."""
    chunks = chunk_text(PARAGRAPHS, max_chars=300, overlap_chars=50)

    assert chunks[0].start_offset == 0
    assert chunks[-1].end_offset == len(PARAGRAPHS.rstrip())
    # Consecutive chunks touch or overlap — never leave a gap.
    for previous, current in pairwise(chunks):
        assert current.start_offset <= previous.end_offset


def test_overlap_repeats_content_between_neighbours() -> None:
    """A fact straddling a boundary must survive in at least one chunk."""
    chunks = chunk_text(PARAGRAPHS, max_chars=400, overlap_chars=120)

    assert len(chunks) > 1
    overlaps = [
        previous.end_offset - current.start_offset for previous, current in pairwise(chunks)
    ]
    assert all(overlap > 0 for overlap in overlaps)


def test_zero_overlap_is_permitted() -> None:
    chunks = chunk_text(PARAGRAPHS, max_chars=300, overlap_chars=0)

    assert len(chunks) > 1
    for chunk in chunks:
        assert PARAGRAPHS[chunk.start_offset : chunk.end_offset] == chunk.text


def test_split_prefers_a_paragraph_break() -> None:
    text = "First clause about upgrades.\n\nSecond clause about refunds and credits."

    chunks = chunk_text(text, max_chars=45, overlap_chars=0)

    assert chunks[0].text == "First clause about upgrades."


def test_split_falls_back_to_a_sentence_end() -> None:
    text = "First sentence about upgrades. Second sentence about refunds and credits here."

    chunks = chunk_text(text, max_chars=45, overlap_chars=0)

    assert chunks[0].text == "First sentence about upgrades."


def test_split_falls_back_to_a_word_boundary() -> None:
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"

    chunks = chunk_text(text, max_chars=30, overlap_chars=0)

    # No chunk ends mid-word.
    for chunk in chunks:
        assert not chunk.text.endswith("-")
        assert chunk.text == chunk.text.strip()
    assert all(" " in chunk.text for chunk in chunks)


def test_unbreakable_text_still_terminates() -> None:
    """A long token with no boundary must not loop forever or grow unbounded."""
    text = "x" * 1000

    chunks = chunk_text(text, max_chars=100, overlap_chars=10)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 100 for chunk in chunks)


@pytest.mark.parametrize(
    ("max_chars", "overlap_chars"),
    [(0, 0), (-1, 0), (100, -1), (100, 100), (100, 150)],
)
def test_invalid_parameters_are_rejected(max_chars: int, overlap_chars: int) -> None:
    """overlap >= max would never advance; both are rejected up front."""
    with pytest.raises(ChunkingError):
        chunk_text("some text", max_chars=max_chars, overlap_chars=overlap_chars)


def test_chunk_length_matches_its_span() -> None:
    chunk = Chunk(index=0, text="abcd", start_offset=10, end_offset=14)

    assert chunk.length == 4
