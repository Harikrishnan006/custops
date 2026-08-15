"""Transport schemas.

Pydantic models describing what crosses the HTTP boundary. Kept separate from
domain models so the wire contract can be versioned independently of the
database schema.
"""

from __future__ import annotations
