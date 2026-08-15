"""The provisioning boundary and its error mapping.

The stub client and the error-code translation are pure, so the surrounding
behaviour — including the divergence §14 exists to catch — is verifiable with no
browser and no portal.
"""

from __future__ import annotations

import pytest

from custops.mcp.tools.enterprise import _PROVISIONING_ERROR_CODES
from custops.mcp.tools.results import ToolErrorCode
from custops.provisioning.client import (
    ProvisioningClient,
    ProvisioningError,
    ProvisioningErrorCode,
    ProvisioningResult,
    StubProvisioningClient,
)

ACCOUNT = "11111111-1111-1111-1111-111111111111"


class TestStubClient:
    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(StubProvisioningClient(), ProvisioningClient)

    async def test_setting_a_tier_confirms_what_was_requested(self) -> None:
        client = StubProvisioningClient()

        result = await client.set_tier(account_id=ACCOUNT, tier="enterprise", seats=20)

        assert result.confirmed_tier == "enterprise"
        assert result.matches_request

    async def test_the_tier_is_readable_afterwards(self) -> None:
        client = StubProvisioningClient()
        await client.set_tier(account_id=ACCOUNT, tier="enterprise", seats=20)

        assert await client.read_tier(account_id=ACCOUNT) == "enterprise"

    async def test_an_unprovisioned_account_reads_as_none(self) -> None:
        """Never provisioned is different from provisioned-to-something-else."""
        assert await StubProvisioningClient().read_tier(account_id=ACCOUNT) is None

    async def test_operations_are_recorded(self) -> None:
        client = StubProvisioningClient()

        await client.set_tier(account_id=ACCOUNT, tier="enterprise", seats=1)
        await client.read_tier(account_id=ACCOUNT)

        assert [call["op"] for call in client.calls] == ["set_tier", "read_tier"]


class TestForcedDivergence:
    async def test_a_portal_that_confirms_a_different_tier_is_detected(self) -> None:
        """§11 asks for a test that forces exactly this divergence.

        The form submits, the portal reports success, and it provisioned
        something else. Echoing the request instead of reading the page back
        would hide it.
        """
        client = StubProvisioningClient(drift_to="professional")

        result = await client.set_tier(account_id=ACCOUNT, tier="enterprise", seats=20)

        assert result.requested_tier == "enterprise"
        assert result.confirmed_tier == "professional"
        assert not result.matches_request

    async def test_the_drifted_tier_is_what_a_later_read_returns(self) -> None:
        client = StubProvisioningClient(drift_to="professional")
        await client.set_tier(account_id=ACCOUNT, tier="enterprise", seats=20)

        assert await client.read_tier(account_id=ACCOUNT) == "professional"


class TestFailures:
    @pytest.mark.parametrize(
        "code",
        [
            ProvisioningErrorCode.LOGIN_FAILED,
            ProvisioningErrorCode.TIMEOUT,
            ProvisioningErrorCode.BROWSER_UNAVAILABLE,
            ProvisioningErrorCode.ACCOUNT_NOT_FOUND,
        ],
    )
    async def test_scripted_failures_raise(self, code: ProvisioningErrorCode) -> None:
        client = StubProvisioningClient(fail_with=code)

        with pytest.raises(ProvisioningError) as error:
            await client.set_tier(account_id=ACCOUNT, tier="enterprise", seats=1)

        assert error.value.code == code

    async def test_a_read_can_fail_too(self) -> None:
        """A portal that cannot be read is not a portal that agrees."""
        client = StubProvisioningClient(fail_with=ProvisioningErrorCode.TIMEOUT)

        with pytest.raises(ProvisioningError):
            await client.read_tier(account_id=ACCOUNT)


class TestErrorMapping:
    def test_every_portal_error_maps_to_a_tool_code(self) -> None:
        """An unmapped code would silently become a generic upstream error."""
        assert set(_PROVISIONING_ERROR_CODES) == set(ProvisioningErrorCode)

    def test_a_timeout_is_retryable(self) -> None:
        """A slow portal is worth trying again; a rejected tier is not."""
        assert _PROVISIONING_ERROR_CODES[ProvisioningErrorCode.TIMEOUT] in (
            ToolErrorCode.UPSTREAM_TIMEOUT,
            ToolErrorCode.UPSTREAM_ERROR,
        )

    def test_a_missing_account_is_not_retryable(self) -> None:
        assert (
            _PROVISIONING_ERROR_CODES[ProvisioningErrorCode.ACCOUNT_NOT_FOUND]
            == ToolErrorCode.NOT_FOUND
        )

    def test_a_rejected_tier_is_a_precondition_failure(self) -> None:
        assert (
            _PROVISIONING_ERROR_CODES[ProvisioningErrorCode.TIER_REJECTED]
            == ToolErrorCode.PRECONDITION_FAILED
        )


class TestResultShape:
    def test_matches_request_compares_confirmation_to_request(self) -> None:
        agreeing = ProvisioningResult(
            account_id=ACCOUNT,
            requested_tier="enterprise",
            confirmed_tier="enterprise",
            seats=1,
            confirmation_text="ok",
        )
        diverging = ProvisioningResult(
            account_id=ACCOUNT,
            requested_tier="enterprise",
            confirmed_tier="starter",
            seats=1,
            confirmation_text="ok",
        )

        assert agreeing.matches_request
        assert not diverging.matches_request
