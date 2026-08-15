"""Structured logging, execution context, audit and health probing.

This package owns the observability contract described in BUILD_SPEC §16: one
``execution_id`` propagated through every log line, tool call, agent run and
audit event, and structured events rather than prose.
"""

from __future__ import annotations
