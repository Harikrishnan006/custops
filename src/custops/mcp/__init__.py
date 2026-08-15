"""MCP: agent-to-tool communication (BUILD_SPEC §8).

Standardised, narrowly scoped access to enterprise capabilities. Agents get **no
arbitrary database access** — every reach into a system of record goes through a
typed tool with a declared permission, an audit record, and (for mutations) an
independently verified approval.

Note the package lives at ``custops.mcp``, not top-level ``mcp``, so it cannot
shadow the MCP SDK's own import name. See ADR-001.
"""

from __future__ import annotations
