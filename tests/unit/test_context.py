"""Correlation context behaviour."""

from __future__ import annotations

import asyncio

from custops.observability.context import (
    bind_context,
    get_execution_id,
    get_request_id,
    new_id,
    sanitize_correlation_id,
)


def test_identifiers_are_unset_by_default() -> None:
    assert get_execution_id() is None
    assert get_request_id() is None


def test_context_is_restored_after_an_exception() -> None:
    try:
        with bind_context(execution_id="exec-1"):
            assert get_execution_id() == "exec-1"
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    # A failed workflow must not leave its id bound to whatever runs next.
    assert get_execution_id() is None


def test_nested_binding_restores_the_outer_value() -> None:
    with bind_context(execution_id="outer"):
        with bind_context(execution_id="inner"):
            assert get_execution_id() == "inner"
        assert get_execution_id() == "outer"


async def test_concurrent_tasks_do_not_share_identifiers() -> None:
    """The property that makes contextvars the right tool for concurrent workflows."""
    observed: dict[str, str | None] = {}

    async def workflow(execution_id: str, delay: float) -> None:
        with bind_context(execution_id=execution_id):
            await asyncio.sleep(delay)
            observed[execution_id] = get_execution_id()

    await asyncio.gather(workflow("exec-a", 0.02), workflow("exec-b", 0.01))

    assert observed == {"exec-a": "exec-a", "exec-b": "exec-b"}


def test_sanitize_strips_characters_that_could_corrupt_a_log_stream() -> None:
    assert sanitize_correlation_id("req-123") == "req-123"
    assert sanitize_correlation_id("req\n123 injected") == "req123injected"
    assert sanitize_correlation_id("a" * 200) == "a" * 64
    # An id consisting entirely of rejected characters yields a fresh one rather
    # than an empty string, so correlation is never silently lost.
    assert sanitize_correlation_id("   ") != ""


def test_new_id_is_unique() -> None:
    assert new_id() != new_id()
