"""MCP server assembly.

Built against the installed SDK's real surface rather than a recalled one: in
`mcp` 2.x the server class is ``MCPServer`` from ``mcp.server`` and tools are
registered with the ``@server.tool(...)`` decorator. (``FastMCP``, the class an
older memory would reach for, does not exist in this version — which is why
Rule 24 says look it up.)

Every registered tool is a thin wrapper that opens a session and delegates to
``execute_tool``. The wrapper does no business logic and no checking of its own,
so the permission / approval / audit path cannot be bypassed by adding a tool
here.

**The role is supplied by the caller and is trusted only as far as the transport
is.** Over stdio the caller is a local process this system launched. Exposing
this server over HTTP without authenticating the caller would make the role
claim self-asserted, and the permission matrix meaningless — that is Phase 13's
problem, and it is called out here so it is not discovered later.
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server import MCPServer

from custops import __version__
from custops.config import Settings, get_settings
from custops.db.engine import create_database
from custops.mcp.permissions.matrix import Role, ToolName
from custops.mcp.tools import enterprise as handlers
from custops.mcp.tools.runtime import ToolContext, execute_tool
from custops.mcp.tools.schemas import (
    AccountInput,
    GetCustomerInput,
    GetPricingInput,
    GetSupportHistoryInput,
    SearchKnowledgeInput,
    UpdateCrmInput,
    UpdateSubscriptionInput,
)
from custops.observability.logging import configure_logging, get_logger
from custops.providers.registry import get_embedding_provider

logger = get_logger(__name__)

INSTRUCTIONS = """\
Customer operations tools over the enterprise systems of record.

Read tools return structured data with source references. Mutating tools require
an approval record for the current execution and the specific entity being
changed; without one they refuse. Every call is audited.
"""


def build_server(settings: Settings | None = None) -> MCPServer:
    """Construct the MCP server with every tool registered."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)

    database = create_database(resolved)
    provider = get_embedding_provider(resolved)
    search_handler = handlers.make_search_knowledge(provider)

    server = MCPServer(
        name="custops-enterprise",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    async def _run(
        tool: str,
        role: str,
        execution_id: str | None,
        arguments: Any,
        handler: Any,
        approval_entity: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """One session per call, committed once.

        A single commit is safe because ``execute_tool`` runs the approval check
        and the handler inside a savepoint: a failure rolls that savepoint back,
        leaving only the audit rows to commit. So the attempt is always
        recorded, and a half-applied mutation never is.
        """
        async with database.session_factory() as session:
            context = ToolContext(
                session=session,
                role=role,
                execution_id=uuid.UUID(execution_id) if execution_id else None,
            )
            result = await execute_tool(
                context, tool, arguments, handler, approval_entity=approval_entity
            )
            await session.commit()
            return result.model_dump(mode="json")

    # --- Read tools -------------------------------------------------------

    @server.tool(
        name=ToolName.GET_CUSTOMER, description="Look up a customer by external reference."
    )
    async def get_customer(
        external_ref: str, role: str = Role.RESEARCH, execution_id: str | None = None
    ) -> dict[str, Any]:
        return await _run(
            ToolName.GET_CUSTOMER,
            role,
            execution_id,
            GetCustomerInput(external_ref=external_ref),
            handlers.get_customer,
        )

    @server.tool(
        name=ToolName.GET_SUBSCRIPTION,
        description="The account's active subscription, as billing sees it.",
    )
    async def get_subscription(
        account_id: str, role: str = Role.RESEARCH, execution_id: str | None = None
    ) -> dict[str, Any]:
        return await _run(
            ToolName.GET_SUBSCRIPTION,
            role,
            execution_id,
            AccountInput(account_id=uuid.UUID(account_id)),
            handlers.get_subscription,
        )

    @server.tool(name=ToolName.GET_CONTRACT, description="The contract currently in force.")
    async def get_contract(
        account_id: str, role: str = Role.RESEARCH, execution_id: str | None = None
    ) -> dict[str, Any]:
        return await _run(
            ToolName.GET_CONTRACT,
            role,
            execution_id,
            AccountInput(account_id=uuid.UUID(account_id)),
            handlers.get_contract,
        )

    @server.tool(name=ToolName.GET_PRICING, description="Plan pricing by plan code.")
    async def get_pricing(
        plan_code: str, role: str = Role.RESEARCH, execution_id: str | None = None
    ) -> dict[str, Any]:
        return await _run(
            ToolName.GET_PRICING,
            role,
            execution_id,
            GetPricingInput(plan_code=plan_code),
            handlers.get_pricing,
        )

    @server.tool(name=ToolName.GET_INVOICE, description="Invoices and past-due count.")
    async def get_invoice(
        account_id: str, role: str = Role.RESEARCH, execution_id: str | None = None
    ) -> dict[str, Any]:
        return await _run(
            ToolName.GET_INVOICE,
            role,
            execution_id,
            AccountInput(account_id=uuid.UUID(account_id)),
            handlers.get_invoice,
        )

    @server.tool(
        name=ToolName.GET_SUPPORT_HISTORY, description="Aggregate support posture for an account."
    )
    async def get_support_history(
        account_id: str,
        limit: int = 20,
        role: str = Role.RESEARCH,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            ToolName.GET_SUPPORT_HISTORY,
            role,
            execution_id,
            GetSupportHistoryInput(account_id=uuid.UUID(account_id), limit=limit),
            handlers.get_support_history,
        )

    @server.tool(
        name=ToolName.SEARCH_KNOWLEDGE,
        description=(
            "Search policies and contracts. Returns structured evidence with source "
            "references and a sufficiency verdict. Pass account_id to include that "
            "account's contracts; omit it for policies only."
        ),
    )
    async def search_knowledge(
        query: str,
        limit: int = 5,
        account_id: str | None = None,
        role: str = Role.RESEARCH,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            ToolName.SEARCH_KNOWLEDGE,
            role,
            execution_id,
            SearchKnowledgeInput(
                query=query,
                limit=limit,
                account_id=uuid.UUID(account_id) if account_id else None,
            ),
            search_handler,
        )

    # --- Mutating tools ---------------------------------------------------

    @server.tool(
        name=ToolName.UPDATE_SUBSCRIPTION,
        description=(
            "Move a subscription to a different plan. Requires an approved, unconsumed "
            "approval record for this execution and this subscription."
        ),
    )
    async def update_subscription(
        subscription_id: str,
        target_plan_code: str,
        execution_id: str,
        role: str = Role.EXECUTION,
    ) -> dict[str, Any]:
        return await _run(
            ToolName.UPDATE_SUBSCRIPTION,
            role,
            execution_id,
            UpdateSubscriptionInput(
                subscription_id=uuid.UUID(subscription_id), target_plan_code=target_plan_code
            ),
            handlers.update_subscription,
            approval_entity=("subscription", subscription_id),
        )

    @server.tool(
        name=ToolName.UPDATE_CRM,
        description=(
            "Sync the CRM's cached plan for an account. Requires an approved, unconsumed "
            "approval record for this execution and this account."
        ),
    )
    async def update_crm(
        account_id: str,
        plan_code: str,
        execution_id: str,
        role: str = Role.EXECUTION,
    ) -> dict[str, Any]:
        return await _run(
            ToolName.UPDATE_CRM,
            role,
            execution_id,
            UpdateCrmInput(account_id=uuid.UUID(account_id), plan_code=plan_code),
            handlers.update_crm,
            approval_entity=("account", account_id),
        )

    # Count what is actually registered, not how many names the enum declares —
    # create_refund and send_notification are deliberately not implemented yet,
    # and a log line claiming 11 would hide that.
    registered = len(registered_tool_names(server))
    logger.info("mcp_server_built", tools_registered=registered, version=__version__)
    return server


def registered_tool_names(server: MCPServer) -> list[str]:
    """Names of the tools registered on ``server``.

    Reads the registry the decorator populated rather than a hand-maintained
    list, so it cannot drift from what was actually registered.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - SDK internals changed
        return []
    return sorted(getattr(manager, "_tools", {}))


def main() -> None:  # pragma: no cover - process entry point
    """Run over stdio, the transport whose caller identity we can trust."""
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
