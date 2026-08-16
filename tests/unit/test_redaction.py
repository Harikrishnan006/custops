"""What must never reach an audit row.

Two prohibitions from §16 with very different failure modes. Storing a secret is
a breach. Storing chain-of-thought is a spec violation and a liability — it is
the model's deliberation, kept in a table an HTTP endpoint serves.

Both are enforced in one module precisely so these tests can be exhaustive about
it rather than sampling eighteen call sites.
"""

from __future__ import annotations

from custops.observability.redaction import (
    CHAIN_OF_THOUGHT_KEYS,
    DROPPED_MARKER,
    MAX_COLLECTION_ITEMS,
    MAX_DEPTH,
    MAX_STRING_LENGTH,
    REDACTED,
    SECRET_KEYS,
    redact,
)

# ----------------------------------------------------------- chain-of-thought


def test_every_declared_reasoning_key_is_dropped() -> None:
    """Exhaustive over the deny-list, so adding a key without handling it fails."""
    payload = dict.fromkeys(CHAIN_OF_THOUGHT_KEYS, "the model deliberating at length")

    cleaned = redact(payload)

    for key in CHAIN_OF_THOUGHT_KEYS:
        assert key not in cleaned


def test_reasoning_is_dropped_rather_than_masked() -> None:
    """A masked key still says the content exists somewhere.

    For a credential that is useful — an auditor wants to know a token was
    present. For chain-of-thought it is the wrong signal entirely: the platform
    must not retain it, so the key goes.
    """
    cleaned = redact({"reasoning": "step one, then step two", "outcome": "eligible"})

    assert "reasoning" not in cleaned
    assert REDACTED not in cleaned.values()
    assert cleaned["outcome"] == "eligible"


def test_dropping_is_recorded_without_recording_what_was_dropped() -> None:
    """An auditor must be able to tell a redacted trace from a thin one."""
    cleaned = redact({"thought": "...", "decision": "approved"})

    assert cleaned[DROPPED_MARKER] == ["thought"]
    assert "..." not in str(cleaned)


def test_reasoning_nested_deep_is_still_dropped() -> None:
    """The dangerous key is rarely at the top level.

    A raw completion three dictionaries down inside a tool result is exactly
    where this ends up in practice.
    """
    cleaned = redact({"tool": {"result": {"data": {"raw_completion": "I think that..."}}}})

    assert "I think" not in str(cleaned)
    assert cleaned["tool"]["result"]["data"][DROPPED_MARKER] == ["raw_completion"]


def test_reasoning_inside_a_list_of_objects_is_dropped() -> None:
    cleaned = redact({"steps": [{"name": "decide", "scratchpad": "weighing options"}]})

    assert "weighing options" not in str(cleaned)


def test_rationale_summary_survives() -> None:
    """§16 explicitly permits a concise rationale. Over-redacting would strip
    the one explanatory field the spec asks for."""
    cleaned = redact({"rationale_summary": "Blocked by outstanding_past_due_invoices."})

    assert cleaned["rationale_summary"] == "Blocked by outstanding_past_due_invoices."


def test_matching_is_case_and_whitespace_insensitive() -> None:
    # Distinctive sentinels: a single letter would collide with the marker key
    # itself ('y' appears in "__redacted_keys__"), making the assertion pass or
    # fail for reasons unrelated to redaction.
    cleaned = redact({"  Reasoning  ": "SENTINEL_ONE", "THOUGHT": "SENTINEL_TWO"})

    assert "SENTINEL_ONE" not in str(cleaned)
    assert "SENTINEL_TWO" not in str(cleaned)
    assert sorted(cleaned[DROPPED_MARKER]) == ["  Reasoning  ", "THOUGHT"]


# --------------------------------------------------------------------- secrets


def test_every_declared_secret_key_is_masked() -> None:
    payload = dict.fromkeys(SECRET_KEYS, "hunter2")

    cleaned = redact(payload)

    assert "hunter2" not in str(cleaned)
    for key in SECRET_KEYS:
        assert cleaned[key] == REDACTED


def test_secrets_are_masked_not_dropped() -> None:
    """That a request carried an authorization header is worth auditing."""
    cleaned = redact({"authorization": "Bearer abc123"})

    assert cleaned["authorization"] == REDACTED
    assert "abc123" not in str(cleaned)


def test_qualified_secret_names_are_matched() -> None:
    """Real payloads namespace their keys."""
    cleaned = redact({"portal_password": "x", "postgres.password": "y", "db_api_key": "z"})

    assert cleaned["portal_password"] == REDACTED
    assert cleaned["postgres.password"] == REDACTED
    assert cleaned["db_api_key"] == REDACTED


def test_a_key_that_merely_contains_a_secret_word_is_kept() -> None:
    """``password_policy_version`` is a fact about configuration, not a credential.

    A substring test would swallow it, making the trace poorer for no security
    gain — which is why matching is on word boundaries.
    """
    cleaned = redact({"password_policy_version": 3, "token_count": 42})

    assert cleaned["password_policy_version"] == 3
    assert cleaned["token_count"] == 42


def test_secrets_nested_in_tool_arguments_are_masked() -> None:
    cleaned = redact({"arguments": {"portal": {"username": "ops", "password": "s3cret"}}})

    assert "s3cret" not in str(cleaned)
    assert cleaned["arguments"]["portal"]["username"] == "ops"


# ----------------------------------------------------------------- bounding


def test_long_strings_are_truncated() -> None:
    """An audit payload is a summary, not a dumping ground for tool output."""
    cleaned = redact({"content": "x" * (MAX_STRING_LENGTH + 500)})

    assert len(cleaned["content"]) < MAX_STRING_LENGTH + 100
    assert cleaned["content"].endswith("[truncated]")


def test_short_strings_are_untouched() -> None:
    assert redact({"a": "hello"})["a"] == "hello"


def test_large_collections_are_capped_and_say_so() -> None:
    cleaned = redact({"items": list(range(MAX_COLLECTION_ITEMS + 20))})

    assert len(cleaned["items"]) == MAX_COLLECTION_ITEMS + 1
    assert "20 more" in str(cleaned["items"][-1])


def test_deep_nesting_stops_rather_than_recursing_forever() -> None:
    """An unbounded structure is itself worth seeing, so it is marked not dropped."""
    payload: dict[str, object] = {"level": "bottom"}
    for _ in range(MAX_DEPTH + 4):
        payload = {"nested": payload}

    assert "max depth exceeded" in str(redact(payload))


def test_a_self_referencing_structure_does_not_hang() -> None:
    """Depth bounding is what makes a cycle survivable."""
    payload: dict[str, object] = {}
    payload["self"] = payload

    assert "max depth exceeded" in str(redact(payload))


# --------------------------------------------------------------- pass-through


def test_scalars_survive_unchanged() -> None:
    cleaned = redact({"count": 3, "ratio": 0.5, "ok": True, "missing": None})

    assert cleaned == {"count": 3, "ratio": 0.5, "ok": True, "missing": None}


def test_unserialisable_values_become_strings_rather_than_failing() -> None:
    """A Decimal or UUID that reached a payload must not fail the JSONB insert
    at commit time, far from whatever put it there."""
    from decimal import Decimal
    from uuid import UUID

    cleaned = redact({"amount": Decimal("12.34"), "id": UUID(int=1)})

    assert cleaned["amount"] == "12.34"
    assert cleaned["id"].endswith("0001")


def test_redaction_does_not_mutate_the_caller_s_payload() -> None:
    """The caller may still be using the dict it passed."""
    original = {"password": "keepme", "nested": {"reasoning": "x"}}

    redact(original)

    assert original["password"] == "keepme"
    assert original["nested"] == {"reasoning": "x"}


def test_an_empty_payload_stays_empty() -> None:
    assert redact({}) == {}
