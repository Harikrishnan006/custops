"""The provisioning boundary.

One protocol, two implementations: a real browser driver and a labelled test
double. The boundary matters more than either — it is what keeps Playwright out
of the tool layer, the graph, and the domain, and it is what lets the whole
Billing → CRM → Provisioning → Validation path be exercised without a browser.

Both operations exist for a reason §14 insists on: ``set_tier`` performs the
change, and ``read_tier`` re-reads it **from the portal**. The Validator uses
``read_tier`` rather than querying the entitlements table, because reading the
table would bypass the very system whose behaviour is in question — and a
validator that checks its own side of an integration proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProvisioningErrorCode(StrEnum):
    """Why a portal interaction failed, in terms the tool layer can map."""

    LOGIN_FAILED = "login_failed"
    ACCOUNT_NOT_FOUND = "account_not_found"
    TIER_REJECTED = "tier_rejected"
    CONFIRMATION_MISSING = "confirmation_missing"
    TIMEOUT = "timeout"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    UNEXPECTED = "unexpected"


class ProvisioningError(RuntimeError):
    """A portal interaction that did not complete."""

    def __init__(self, code: ProvisioningErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    """What the portal reported after a tier change.

    ``confirmed_tier`` is scraped from the page *after* submission — the portal's
    own account of what it did, not an echo of what was asked for. A form that
    submits successfully and provisions something else is exactly the failure
    this field exists to expose.
    """

    account_id: str
    requested_tier: str
    confirmed_tier: str
    seats: int
    confirmation_text: str

    @property
    def matches_request(self) -> bool:
        return self.confirmed_tier == self.requested_tier


@runtime_checkable
class ProvisioningClient(Protocol):
    """Drives the legacy portal."""

    async def set_tier(self, *, account_id: str, tier: str, seats: int) -> ProvisioningResult:
        """Log in, find the account, submit the tier change, read the confirmation."""
        ...

    async def read_tier(self, *, account_id: str) -> str | None:
        """Read the currently provisioned tier from the portal itself."""
        ...


@dataclass
class StubProvisioningClient:
    """A scripted stand-in for the portal.

    **A test double, not a portal.** It performs no browser work and proves
    nothing about whether the real form submits. Its purpose is to make the
    surrounding path — approval enforcement, tool auditing, node wiring,
    cross-system validation — exercisable without a browser, exactly as the
    deterministic embedder and chat provider do for their layers.

    ``fail_with`` scripts a failure so the unhappy paths are testable, and
    ``drift_to`` simulates a portal that confirms a tier different from the one
    requested — the divergence §14 exists to catch, forced deliberately.
    """

    tiers: dict[str, str] = field(default_factory=dict)
    seats: dict[str, int] = field(default_factory=dict)
    fail_with: ProvisioningErrorCode | None = None
    drift_to: str | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def set_tier(self, *, account_id: str, tier: str, seats: int) -> ProvisioningResult:
        self.calls.append({"op": "set_tier", "account_id": account_id, "tier": tier})
        if self.fail_with is not None:
            raise ProvisioningError(self.fail_with, f"Scripted failure: {self.fail_with}.")

        confirmed = self.drift_to or tier
        self.tiers[account_id] = confirmed
        self.seats[account_id] = seats
        return ProvisioningResult(
            account_id=account_id,
            requested_tier=tier,
            confirmed_tier=confirmed,
            seats=seats,
            confirmation_text=f"Provisioned {confirmed}",
        )

    async def read_tier(self, *, account_id: str) -> str | None:
        self.calls.append({"op": "read_tier", "account_id": account_id})
        if self.fail_with is not None:
            raise ProvisioningError(self.fail_with, f"Scripted failure: {self.fail_with}.")
        return self.tiers.get(account_id)
