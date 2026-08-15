"""Deterministic text chunking.

Three properties are deliberate, and each is asserted in the tests:

1. **Deterministic.** The same document always chunks identically. Re-ingesting
   an unchanged policy must not produce a different corpus, or every retrieval
   test becomes a coin flip and no evaluation is reproducible.

2. **Offsets are exact.** Every chunk carries the character span it occupies in
   the source, and ``source[start:end]`` reproduces the chunk verbatim. This is
   what lets an approval request cite "policy DIS-002, characters 340-612"
   rather than asserting a paraphrase and hoping the reader trusts it.

3. **Boundaries are preferred, not forced.** Splits are made at a paragraph
   break where one is available near the limit, then a sentence end, then a word
   boundary, then — only if nothing else exists — mid-word. A policy clause cut
   in half retrieves poorly and reads worse when quoted back to a human.

No token-based splitting: token counts depend on the model's tokenizer, so a
token-sized chunker silently re-chunks the whole corpus when the embedding model
changes. Characters are stable, and the limit is set conservatively enough that
chunks fit any current embedding context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150

# How far back from the hard limit a nicer boundary is worth taking. Beyond
# this the chunk gets too short and retrieval quality suffers more than the
# ragged edge costs.
_BOUNDARY_SEARCH_WINDOW = 300

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s")


class ChunkingError(ValueError):
    """Raised when chunking parameters cannot produce progress."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable span of a document."""

    index: int
    text: str
    start_offset: int
    end_offset: int

    @property
    def length(self) -> int:
        return self.end_offset - self.start_offset


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split ``text`` into overlapping chunks with exact source offsets.

    Overlap exists because a fact can straddle a boundary: a clause whose
    subject is in one chunk and whose condition is in the next retrieves badly
    from either. Repeating a tail of the previous chunk keeps such pairs intact
    at the cost of some duplication.
    """
    if max_chars < 1:
        raise ChunkingError(f"max_chars must be positive, got {max_chars}")
    if overlap_chars < 0:
        raise ChunkingError(f"overlap_chars must not be negative, got {overlap_chars}")
    if overlap_chars >= max_chars:
        # Otherwise each chunk starts at or before the previous one and the
        # loop never terminates.
        raise ChunkingError(
            f"overlap_chars ({overlap_chars}) must be smaller than max_chars ({max_chars})"
        )

    if not text.strip():
        return []

    chunks: list[Chunk] = []
    position = 0
    length = len(text)

    while position < length:
        hard_end = min(position + max_chars, length)
        end = hard_end if hard_end >= length else _find_boundary(text, position, hard_end)

        raw = text[position:end]
        stripped = raw.strip()
        if stripped:
            # Report the span of the *stripped* text so offsets point at
            # content rather than at surrounding whitespace.
            leading = len(raw) - len(raw.lstrip())
            start_offset = position + leading
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=stripped,
                    start_offset=start_offset,
                    end_offset=start_offset + len(stripped),
                )
            )

        if end >= length:
            break

        next_position = end - overlap_chars
        # Guarantee forward progress even if a boundary landed early.
        position = max(next_position, position + 1)

    return chunks


def _find_boundary(text: str, start: int, hard_end: int) -> int:
    """Pick the nicest split point at or before ``hard_end``."""
    window_start = max(start + 1, hard_end - _BOUNDARY_SEARCH_WINDOW)
    window = text[window_start:hard_end]

    paragraph = _last_match_end(_PARAGRAPH_BREAK, window)
    if paragraph is not None:
        return window_start + paragraph

    sentence = _last_match_end(_SENTENCE_END, window)
    if sentence is not None:
        return window_start + sentence

    space = window.rfind(" ")
    if space != -1:
        return window_start + space + 1

    # No boundary anywhere in the window (a long unbroken token). Split at the
    # limit rather than growing the chunk without bound.
    return hard_end


def _last_match_end(pattern: re.Pattern[str], window: str) -> int | None:
    last: int | None = None
    for match in pattern.finditer(window):
        last = match.end()
    return last
