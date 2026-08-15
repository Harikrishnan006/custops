"""Redis client construction and probing.

See docs/decisions/ADR-003-redis-role.md: Redis is present as a declared
architectural dependency, but its concrete job is an open question resolved at
Phase 5. It must earn one or be removed.
"""

from __future__ import annotations
