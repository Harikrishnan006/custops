"""The tool permission matrix and result envelope."""

from __future__ import annotations

import uuid

import pytest

from custops.mcp.permissions.matrix import (
    PERMISSION_MATRIX,
    PermissionDeniedError,
    Role,
    ToolName,
    UnknownToolError,
    check_permission,
    get_policy,
    is_mutating,
    tools_for_role,
)
from custops.mcp.tools.results import (
    RETRYABLE_CODES,
    ToolError,
    ToolErrorCode,
    ToolResult,
)
from custops.mcp.tools.schemas import CustomerOutput


class TestMatrixShape:
    def test_every_declared_tool_has_a_policy(self) -> None:
        """A tool without a matrix entry must break loudly, not default."""
        assert set(PERMISSION_MATRIX) >= {
            ToolName.GET_CUSTOMER,
            ToolName.GET_SUBSCRIPTION,
            ToolName.UPDATE_SUBSCRIPTION,
            ToolName.UPDATE_CRM,
            ToolName.SEARCH_KNOWLEDGE,
        }

    def test_every_mutating_tool_declares_an_approval_action(self) -> None:
        """Otherwise the tool layer has nothing to verify against (D9)."""
        for policy in PERMISSION_MATRIX.values():
            if policy.mutating:
                assert policy.approval_action, f"{policy.tool} is mutating with no action"

    def test_no_read_tool_declares_an_approval_action(self) -> None:
        for policy in PERMISSION_MATRIX.values():
            if not policy.mutating:
                assert policy.approval_action is None

    def test_unknown_tool_fails_closed(self) -> None:
        with pytest.raises(UnknownToolError):
            get_policy("drop_all_tables")


class TestPermissionChecks:
    def test_research_may_read(self) -> None:
        policy = check_permission(Role.RESEARCH, ToolName.GET_CUSTOMER)

        assert not policy.mutating

    def test_research_may_not_mutate(self) -> None:
        """Capability, not approval: no approval can grant Research this."""
        with pytest.raises(PermissionDeniedError) as error:
            check_permission(Role.RESEARCH, ToolName.UPDATE_SUBSCRIPTION)

        assert error.value.role == Role.RESEARCH
        assert error.value.tool == ToolName.UPDATE_SUBSCRIPTION

    def test_validator_may_not_mutate(self) -> None:
        """The Validator verifies state; it must never be able to change it."""
        with pytest.raises(PermissionDeniedError):
            check_permission(Role.VALIDATOR, ToolName.UPDATE_CRM)

    def test_planner_cannot_touch_systems_of_record(self) -> None:
        """The Planner writes plans, not state."""
        for tool in (ToolName.GET_SUBSCRIPTION, ToolName.UPDATE_SUBSCRIPTION):
            with pytest.raises(PermissionDeniedError):
                check_permission(Role.PLANNER, tool)

    def test_execution_may_mutate(self) -> None:
        policy = check_permission(Role.EXECUTION, ToolName.UPDATE_SUBSCRIPTION)

        assert policy.mutating
        assert policy.approval_action == "subscription_upgrade"

    def test_supervisor_cannot_perform_business_actions(self) -> None:
        """§6: the Supervisor must not perform unrestricted business actions."""
        for tool, policy in PERMISSION_MATRIX.items():
            if policy.mutating:
                with pytest.raises(PermissionDeniedError):
                    check_permission(Role.SUPERVISOR, tool)

    def test_billing_specialist_is_read_only(self) -> None:
        """The A2A specialist reasons about pricing; it does not apply changes."""
        readable = tools_for_role(Role.BILLING_SPECIALIST)

        assert ToolName.GET_PRICING in readable
        assert all(not is_mutating(tool) for tool in readable)

    def test_tools_for_role_is_sorted_and_scoped(self) -> None:
        execution = tools_for_role(Role.EXECUTION)

        assert list(execution) == sorted(execution)
        assert ToolName.UPDATE_SUBSCRIPTION in execution
        assert ToolName.GET_CUSTOMER in execution


class TestToolResults:
    def test_success_carries_the_payload(self) -> None:
        payload = CustomerOutput(
            id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            external_ref="ACME",
            name="Acme",
            status="active",
            account_ids=[],
        )

        result = ToolResult.success(payload)

        assert result.ok
        assert result.error is None
        assert result.data is not None

    def test_failure_carries_a_code_not_a_traceback(self) -> None:
        result: ToolResult[CustomerOutput] = ToolResult.failure(
            ToolErrorCode.NOT_FOUND, "No customer 'NOPE'.", external_ref="NOPE"
        )

        assert not result.ok
        assert result.data is None
        assert result.error is not None
        assert result.error.code == ToolErrorCode.NOT_FOUND
        assert result.error.details == {"external_ref": "NOPE"}

    @pytest.mark.parametrize(
        "code",
        [
            ToolErrorCode.PERMISSION_DENIED,
            ToolErrorCode.APPROVAL_REQUIRED,
            ToolErrorCode.NOT_FOUND,
            ToolErrorCode.INVALID_INPUT,
            ToolErrorCode.INTERNAL_ERROR,
        ],
    )
    def test_non_transient_failures_are_not_retryable(self, code: ToolErrorCode) -> None:
        """Retrying a permission denial or a corruption is how damage compounds."""
        assert not ToolError(code=code, message="x").retryable

    @pytest.mark.parametrize("code", [ToolErrorCode.UPSTREAM_TIMEOUT, ToolErrorCode.UPSTREAM_ERROR])
    def test_transient_failures_are_retryable(self, code: ToolErrorCode) -> None:
        assert ToolError(code=code, message="x").retryable

    def test_retryable_set_is_narrow(self) -> None:
        """Unknown failures default to non-retryable, deliberately."""
        assert {
            ToolErrorCode.UPSTREAM_TIMEOUT,
            ToolErrorCode.UPSTREAM_ERROR,
        } == RETRYABLE_CODES
