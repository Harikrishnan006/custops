"""Knowledge: ingestion, chunking, embedding, retrieval, Evidence assembly.

The Research agent returns *structured evidence with source references*, never
prose (BUILD_SPEC §6). Everything in this package exists to make that possible:
a chunk knows exactly which document it came from and which character span it
occupies, so a claim in an approval request can be traced back to the sentence
that supports it.
"""

from __future__ import annotations
