"""What may be written into an audit payload, and what must never be.

Two rules from BUILD_SPEC §16, enforced here rather than trusted to eighteen
call sites:

* **Never chain-of-thought** (Rule 18). Audit rows carry structured decisions,
  evidence references, outcomes and concise rationale summaries — never the
  reasoning that produced them.
* **Never secrets.** Audit rows are read by an inspection endpoint, so anything
  written here is effectively disclosed to whoever can see a trace.

Centralised because the alternative is vigilance. A recorder that redacts on the
way in cannot be bypassed by a caller who forgot; a convention that each call
site sanitises its own payload is one careless dictionary away from writing a
model's deliberation into a table an API serves.

The approach is **deny by key name, then bound by size**. Key-name matching is
crude and deliberately over-broad: a field called ``reasoning`` is dropped even
when it happens to hold something harmless, because the cost of dropping a
useful field is a slightly thinner trace, and the cost of keeping a harmful one
is chain-of-thought in an audit log.
"""

from __future__ import annotations

from typing import Any

# Keys whose *values* are reasoning rather than conclusions. Dropped outright:
# masking would leave a key implying the content exists somewhere, which is
# exactly the wrong signal for something the platform must never retain.
#
# ``rationale_summary`` is deliberately absent — it is a bounded conclusion the
# spec explicitly permits, and the domain models cap its length.
CHAIN_OF_THOUGHT_KEYS = frozenset(
    {
        "chain_of_thought",
        "cot",
        "deliberation",
        "inner_monologue",
        "raw_completion",
        "raw_response",
        "reasoning",
        "reasoning_trace",
        "scratchpad",
        "thinking",
        "thought",
        "thoughts",
    }
)

# Keys whose values are credentials. Masked rather than dropped: knowing that a
# request carried an authorization header is useful to an auditor; knowing its
# value is a breach.
SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "dsn",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session_token",
        "token",
    }
)

REDACTED = "***"
DROPPED_MARKER = "__redacted_keys__"

# A payload is a summary, not a transcript. These bounds stop an audit row from
# becoming a dumping ground for whatever a tool happened to return.
MAX_STRING_LENGTH = 2000
MAX_COLLECTION_ITEMS = 50
MAX_DEPTH = 6
TRUNCATION_SUFFIX = "…[truncated]"


def _is_chain_of_thought(key: str) -> bool:
    return key.strip().lower() in CHAIN_OF_THOUGHT_KEYS


def _is_secret(key: str) -> bool:
    """Match a secret key exactly, or as a suffix of a qualified name.

    ``portal_password`` and ``postgres.password`` must both match, while
    ``password_policy_version`` — a fact about configuration, not a credential —
    must not. Suffix matching on word boundaries gives that distinction; a plain
    substring test would swallow the harmless case.
    """
    lowered = key.strip().lower()
    if lowered in SECRET_KEYS:
        return True
    return any(
        lowered.endswith(f"_{secret}") or lowered.endswith(f".{secret}") for secret in SECRET_KEYS
    )


def redact(payload: Any, *, _depth: int = 0) -> Any:
    """Return a copy of ``payload`` safe to persist and to serve.

    Recursive, because the dangerous key is rarely at the top level — a tool
    result nested three dictionaries deep is exactly where a raw completion
    ends up.
    """
    if _depth >= MAX_DEPTH:
        # Refuse to descend further rather than truncating silently: an
        # unbounded structure is itself worth seeing in the trace.
        return "…[max depth exceeded]"

    if isinstance(payload, dict):
        return _redact_mapping(payload, _depth=_depth)

    if isinstance(payload, (list, tuple)):
        items = list(payload)[:MAX_COLLECTION_ITEMS]
        redacted = [redact(item, _depth=_depth + 1) for item in items]
        if len(payload) > MAX_COLLECTION_ITEMS:
            redacted.append(f"…[{len(payload) - MAX_COLLECTION_ITEMS} more]")
        return redacted

    if isinstance(payload, str):
        return _truncate(payload)

    if isinstance(payload, (int, float, bool)) or payload is None:
        return payload

    # Anything else — UUID, Decimal, datetime, an ORM object someone passed by
    # accident — becomes its string form, then gets bounded. Storing the repr of
    # an arbitrary object is better than failing the JSONB insert at commit,
    # which would surface far from its cause.
    return _truncate(str(payload))


def _redact_mapping(payload: dict[Any, Any], *, _depth: int) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    dropped: list[str] = []

    for raw_key, value in payload.items():
        key = str(raw_key)
        if _is_chain_of_thought(key):
            dropped.append(key)
            continue
        if _is_secret(key):
            cleaned[key] = REDACTED
            continue
        cleaned[key] = redact(value, _depth=_depth + 1)

    if dropped:
        # Record *that* something was withheld, never what. An auditor should be
        # able to tell a redacted trace from a thin one.
        cleaned[DROPPED_MARKER] = sorted(dropped)

    return cleaned


def _truncate(value: str) -> str:
    if len(value) <= MAX_STRING_LENGTH:
        return value
    return value[:MAX_STRING_LENGTH] + TRUNCATION_SUFFIX
