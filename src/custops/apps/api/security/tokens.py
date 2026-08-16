"""Minting, hashing and verifying bearer tokens (§17).

No HTTP here, and no database session: this module is the credential algebra,
which is what lets every rule below be tested without infrastructure.

**The plaintext token is returned exactly once**, from :func:`mint`. It is never
stored, logged, put in an audit payload, or returned by any endpoint. What the
database holds is a SHA-256 hash, and a hash is not a credential.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

# 32 bytes of OS randomness -> 43 URL-safe characters. Far beyond any feasible
# search, which is what makes a fast hash the right choice (see credential.py).
TOKEN_ENTROPY_BYTES = 32

# Prefixed so a leaked string is recognisable as a CustOps credential in a log
# or a paste, and so a secret scanner can be taught one pattern.
TOKEN_PREFIX = "custops_"

AUTH_SCHEME = "Bearer"


class TokenError(ValueError):
    """A supplied token is malformed. Never carries the token itself."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The one moment plaintext exists.

    ``plaintext`` is handed to the operator and then forgotten; ``token_hash``
    is what gets persisted. Keeping them in one object makes the asymmetry
    obvious at the call site: one field is stored, the other is shown once.
    """

    plaintext: str
    token_hash: str

    def __repr__(self) -> str:
        # A repr reaches logs and tracebacks. The plaintext must not.
        return f"IssuedToken(token_hash={self.token_hash[:8]}…, plaintext=<redacted>)"


def mint() -> IssuedToken:
    """Generate a new token and its hash."""
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    return IssuedToken(plaintext=plaintext, token_hash=hash_token(plaintext))


def hash_token(plaintext: str) -> str:
    """Hash a token for storage and lookup.

    Deterministic and unsalted **on purpose**: authentication looks a token up
    by its hash, which a per-row salt would make impossible without scanning
    every row. Safe here only because the input is 256 bits of randomness — the
    same choice would be wrong for a password.
    """
    if not plaintext:
        raise TokenError("An empty token cannot be hashed.")
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def tokens_match(supplied_hash: str, stored_hash: str) -> bool:
    """Compare two hashes in constant time.

    ``==`` on strings short-circuits at the first differing byte, which leaks
    how much of a guess was right. Irrelevant for a random 256-bit token in
    practice, used anyway because credential comparison is the one place where
    reaching for the careful primitive should be automatic.
    """
    return hmac.compare_digest(supplied_hash, stored_hash)


def extract_bearer(header_value: str | None) -> str:
    """Pull the token out of an ``Authorization`` header.

    Raises rather than returning ``None`` for a malformed header, so a caller
    cannot accidentally treat "no credential" and "broken credential" alike.
    """
    if not header_value:
        raise TokenError("No Authorization header was supplied.")

    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME.lower():
        raise TokenError(f"Authorization header must use the {AUTH_SCHEME} scheme.")

    token = parts[1].strip()
    if not token:
        raise TokenError("The Bearer credential was empty.")
    return token


@dataclass(frozen=True, slots=True)
class ValidityCheck:
    """Why a token was accepted or refused.

    A reason code rather than a bare boolean: the endpoint logs *why*
    authentication failed, and "expired" and "revoked" call for very different
    responses from whoever is holding the token.
    """

    valid: bool
    reason: str | None = None


def check_validity(
    *,
    revoked_at: datetime | None,
    expires_at: datetime | None,
    user_is_active: bool,
    now: datetime,
) -> ValidityCheck:
    """Is a token that exists still usable?

    Order matters: revocation is checked first because it is deliberate. A
    revoked token reported as merely "expired" would tell an operator the
    credential aged out when in fact someone withdrew it.
    """
    if revoked_at is not None:
        return ValidityCheck(valid=False, reason="token_revoked")
    if expires_at is not None and expires_at <= now:
        return ValidityCheck(valid=False, reason="token_expired")
    if not user_is_active:
        # Deactivating a person must end their access without anyone having to
        # remember which credentials they hold.
        return ValidityCheck(valid=False, reason="user_inactive")
    return ValidityCheck(valid=True)
