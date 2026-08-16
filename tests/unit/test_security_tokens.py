"""Credential rules (§17).

No HTTP, no database — that is the point of keeping the token algebra separate
from the dependency that uses it. Everything a credential must guarantee is
checkable here, and checked exhaustively.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custops.apps.api.security.tokens import (
    TOKEN_PREFIX,
    IssuedToken,
    TokenError,
    check_validity,
    extract_bearer,
    hash_token,
    mint,
    tokens_match,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------- minting


def test_a_minted_token_is_high_entropy() -> None:
    """32 random bytes. A guessable credential is not a credential."""
    token = mint()

    assert token.plaintext.startswith(TOKEN_PREFIX)
    body = token.plaintext.removeprefix(TOKEN_PREFIX)
    assert len(body) >= 40


def test_every_minted_token_is_unique() -> None:
    tokens = {mint().plaintext for _ in range(200)}

    assert len(tokens) == 200


def test_minting_returns_a_hash_that_matches_the_plaintext() -> None:
    token = mint()

    assert token.token_hash == hash_token(token.plaintext)


def test_the_prefix_makes_a_leaked_token_recognisable() -> None:
    """So a secret scanner can be taught one pattern, and a token pasted into
    a ticket is identifiable as a CustOps credential."""
    assert mint().plaintext.startswith("custops_")


# ------------------------------------------------------------------- hashing


def test_hashing_is_deterministic() -> None:
    """Authentication looks a token up *by* its hash, so the same input must
    always produce the same output — which is why there is no per-row salt."""
    assert hash_token("custops_abc") == hash_token("custops_abc")


def test_different_tokens_hash_differently() -> None:
    assert hash_token("custops_abc") != hash_token("custops_abd")


def test_a_hash_does_not_contain_the_token() -> None:
    """The property the whole design rests on."""
    plaintext = mint().plaintext

    assert plaintext not in hash_token(plaintext)


def test_the_hash_is_the_expected_width() -> None:
    """SHA-256 hex. The column is sized for exactly this."""
    assert len(hash_token("custops_abc")) == 64


def test_an_empty_token_cannot_be_hashed() -> None:
    """Otherwise an empty Authorization header would hash to a stable value
    that someone could register as a token."""
    with pytest.raises(TokenError):
        hash_token("")


def test_the_issued_token_repr_hides_the_plaintext() -> None:
    """A repr reaches logs and tracebacks."""
    token = IssuedToken(plaintext="custops_supersecret", token_hash="a" * 64)

    assert "supersecret" not in repr(token)
    assert "redacted" in repr(token)


# ---------------------------------------------------------------- comparison


def test_matching_hashes_compare_equal() -> None:
    assert tokens_match("a" * 64, "a" * 64)


def test_differing_hashes_do_not_match() -> None:
    assert not tokens_match("a" * 64, "b" * 64)


def test_comparison_of_different_lengths_is_safe() -> None:
    assert not tokens_match("a", "a" * 64)


# ------------------------------------------------------------ header parsing


def test_a_bearer_header_yields_the_token() -> None:
    assert extract_bearer("Bearer custops_abc") == "custops_abc"


def test_the_scheme_is_matched_case_insensitively() -> None:
    """RFC 7235 makes the scheme case-insensitive; clients differ."""
    assert extract_bearer("bearer custops_abc") == "custops_abc"
    assert extract_bearer("BEARER custops_abc") == "custops_abc"


@pytest.mark.parametrize(
    "header",
    [None, "", "custops_abc", "Basic dXNlcjpwYXNz", "Bearer", "Bearer   "],
)
def test_malformed_headers_are_refused(header: str | None) -> None:
    """Raising rather than returning None: "no credential" and "broken
    credential" must not be silently interchangeable."""
    with pytest.raises(TokenError):
        extract_bearer(header)


def test_a_token_containing_spaces_keeps_everything_after_the_scheme() -> None:
    """Splitting on every space would truncate a token; only the first split
    is meaningful."""
    assert extract_bearer("Bearer abc def") == "abc def"


def test_a_parse_error_never_contains_the_token() -> None:
    """Error messages reach logs."""
    try:
        extract_bearer("Basic custops_supersecret")
    except TokenError as error:
        assert "supersecret" not in str(error)


# ------------------------------------------------------------------ validity


def test_a_live_token_is_valid() -> None:
    check = check_validity(
        revoked_at=None, expires_at=NOW + timedelta(days=1), user_is_active=True, now=NOW
    )

    assert check.valid


def test_a_token_without_an_expiry_is_valid() -> None:
    """Null means "does not expire" — explicit, rather than a far-future
    sentinel date that would eventually arrive."""
    check = check_validity(
        revoked_at=None, expires_at=None, user_is_active=True, now=NOW
    )

    assert check.valid


def test_an_expired_token_is_refused() -> None:
    check = check_validity(
        revoked_at=None, expires_at=NOW - timedelta(seconds=1), user_is_active=True, now=NOW
    )

    assert not check.valid
    assert check.reason == "token_expired"


def test_expiry_is_exclusive_at_the_boundary() -> None:
    """A token expiring exactly now is expired. The alternative leaves a
    one-instant window whose behaviour nobody can reason about."""
    check = check_validity(
        revoked_at=None, expires_at=NOW, user_is_active=True, now=NOW
    )

    assert not check.valid


def test_a_revoked_token_is_refused() -> None:
    check = check_validity(
        revoked_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        user_is_active=True,
        now=NOW,
    )

    assert not check.valid
    assert check.reason == "token_revoked"


def test_revocation_is_reported_ahead_of_expiry() -> None:
    """A revoked token reported as merely "expired" would tell an operator the
    credential aged out when in fact someone withdrew it."""
    check = check_validity(
        revoked_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(days=1),
        user_is_active=True,
        now=NOW,
    )

    assert check.reason == "token_revoked"


def test_a_deactivated_user_cannot_authenticate() -> None:
    """Deactivating a person must end their access without anyone having to
    remember which credentials they hold."""
    check = check_validity(
        revoked_at=None, expires_at=None, user_is_active=False, now=NOW
    )

    assert not check.valid
    assert check.reason == "user_inactive"
