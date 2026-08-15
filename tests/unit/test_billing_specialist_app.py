"""The specialist's A2A surface, and the boundary it must not cross.

Everything here runs without PostgreSQL. The endpoints that need billing data
are exercised against a database that cannot connect — which is itself a test
worth having, because the specialist's contract says a failure comes back as a
failed *task* carrying a code, never as a leaked driver message or a 500.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from custops.a2a.client.billing import BillingSpecialistClient, ConsultStatus
from custops.a2a.contracts.card import AGENT_CARD_PATH
from custops.a2a.contracts.pricing import CAPABILITY_PRICING_DECISION
from custops.apps.billing_specialist.app import create_specialist
from custops.config import A2ASettings, LoggingSettings, PostgresSettings, Settings
from custops.db.engine import Database
from custops.mcp.permissions.matrix import (
    PERMISSION_MATRIX,
    Role,
    ToolName,
    tools_for_role,
)

SPECIALIST_URL = "http://specialist.test"


# The text a real driver failure carries. Held here so the leak test has
# something genuine to catch: host, port, database name and the word 'password'
# are exactly what must not reach an agent through an error message.
DRIVER_ERROR_TEXT = (
    'connection to server at "localhost" (127.0.0.1), port 5432 failed: '
    'password authentication failed for user "custops" (database "custops_test")'
)


class _UnreachableSessionFactory:
    """A database the specialist cannot reach.

    Used instead of pointing at a dead port: a real TCP timeout costs eight
    seconds per test on Windows and proves nothing about this code. The failure
    is raised where a driver raises it — on entering the session context — so
    the app's handling is exercised exactly as it would be in production.
    """

    def __call__(self) -> _UnreachableSessionFactory:
        return self

    async def __aenter__(self) -> Any:
        raise OSError(DRIVER_ERROR_TEXT)

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def specialist_settings() -> Settings:
    """Explicit settings; nothing here opens a socket."""
    return Settings(
        _env_file=None,
        environment="test",
        postgres=PostgresSettings(
            _env_file=None,
            host="localhost",
            port=5432,
            user="custops",
            password=SecretStr("unit-test-password"),
            db="custops_test",
        ),
        logging=LoggingSettings(_env_file=None, level="INFO", format="json"),
        a2a=A2ASettings(_env_file=None, billing_specialist_url=SPECIALIST_URL),
    )


@pytest.fixture
def unreachable_db() -> Database:
    """A ``Database`` whose sessions always fail to open."""
    return Database(engine=cast(Any, None), session_factory=cast(Any, _UnreachableSessionFactory()))


@pytest.fixture
async def specialist_client(specialist_settings: Settings, unreachable_db: Database) -> Any:
    app = create_specialist(settings=specialist_settings, database=unreachable_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=SPECIALIST_URL) as client:
        yield client


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": [{"data": payload}],
        }
    }


# ------------------------------------------------------------------- discovery


async def test_the_agent_publishes_its_card_at_the_well_known_path(
    specialist_client: AsyncClient,
) -> None:
    response = await specialist_client.get(AGENT_CARD_PATH)

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "billing-specialist"
    assert card["skills"][0]["id"] == CAPABILITY_PRICING_DECISION


async def test_the_agent_answers_a_liveness_check_without_a_database(
    specialist_client: AsyncClient,
) -> None:
    """Liveness must not depend on the data path.

    A specialist that reports itself down whenever PostgreSQL is slow gives the
    orchestrator no way to tell 'process missing' from 'query slow'.
    """
    response = await specialist_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --------------------------------------------------------------- request shape


async def test_a_message_with_no_json_part_is_a_failed_task_not_an_error(
    specialist_client: AsyncClient,
) -> None:
    response = await specialist_client.post(
        "/message:send", json={"message": {"parts": [{"text": "please price this"}]}}
    )

    assert response.status_code == 200
    task = response.json()
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["artifacts"][0]["parts"][0]["data"]["code"] == "invalid_request"


async def test_a_payload_that_breaks_the_contract_is_refused_by_the_agent(
    specialist_client: AsyncClient,
) -> None:
    """Validation happens at the boundary, before any billing data is touched."""
    response = await specialist_client.post(
        "/message:send", json=_envelope({"account_id": "not-a-uuid"})
    )

    task = response.json()
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["artifacts"][0]["parts"][0]["data"]["code"] == "invalid_request"


async def test_a_json_encoded_text_part_is_accepted(
    specialist_client: AsyncClient,
) -> None:
    """Clients differ on whether they send a data part or a JSON text part."""
    import json

    payload = {"account_id": str(uuid.uuid4()), "target_plan_code": "enterprise"}
    response = await specialist_client.post(
        "/message:send",
        json={"message": {"parts": [{"text": json.dumps(payload)}]}},
    )

    task = response.json()
    # The payload parsed; the failure that follows is the unreachable database,
    # not a rejected request.
    assert task["artifacts"][0]["parts"][0]["data"]["code"] != "invalid_request"


# ------------------------------------------------------------- failure honesty


async def test_an_unreachable_database_produces_a_failed_task_not_a_500(
    specialist_client: AsyncClient,
) -> None:
    """The transport succeeded; the agent could not answer. Those are different."""
    response = await specialist_client.post(
        "/message:send",
        json=_envelope({"account_id": str(uuid.uuid4()), "target_plan_code": "enterprise"}),
    )

    assert response.status_code == 200
    task = response.json()
    assert task["status"]["state"] == "TASK_STATE_FAILED"


async def test_an_internal_failure_does_not_leak_infrastructure_detail(
    specialist_client: AsyncClient,
) -> None:
    """A driver error's text carries connection details.

    An agent reading that message would be reading infrastructure it has no
    business seeing, and would have every incentive to improvise around it.
    """
    response = await specialist_client.post(
        "/message:send",
        json=_envelope({"account_id": str(uuid.uuid4()), "target_plan_code": "enterprise"}),
    )

    message = response.json()["artifacts"][0]["parts"][0]["data"]["message"]
    assert "password" not in message.lower()
    assert "custops_test" not in message
    assert "localhost" not in message


# ------------------------------------------------------------ task retrievable


async def test_a_task_can_be_fetched_by_id_after_it_is_produced(
    specialist_client: AsyncClient,
) -> None:
    created = (
        await specialist_client.post("/message:send", json={"message": {"parts": []}})
    ).json()

    fetched = await specialist_client.get(f"/tasks/{created['id']}")

    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]


async def test_an_unknown_task_id_is_a_404(specialist_client: AsyncClient) -> None:
    response = await specialist_client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_two_apps_do_not_share_task_state(
    specialist_settings: Settings, unreachable_db: Database
) -> None:
    """Task state is per-app, not module-global.

    A module-level dict would also be a data race across the concurrent requests
    this server accepts.
    """
    first = create_specialist(settings=specialist_settings, database=unreachable_db)
    second = create_specialist(settings=specialist_settings, database=unreachable_db)

    async with AsyncClient(transport=ASGITransport(app=first), base_url=SPECIALIST_URL) as client_a:
        task = (await client_a.post("/message:send", json={"message": {"parts": []}})).json()

    async with AsyncClient(
        transport=ASGITransport(app=second), base_url=SPECIALIST_URL
    ) as client_b:
        assert (await client_b.get(f"/tasks/{task['id']}")).status_code == 404


# ------------------------------------------------- the client against the agent


async def test_the_real_client_reads_the_real_agents_refusal(
    specialist_settings: Settings, unreachable_db: Database
) -> None:
    """Client and agent, end to end, with no canned payload in between.

    The two sides are written from the same contract; this is what proves they
    were written from the same *reading* of it.
    """
    app = create_specialist(settings=specialist_settings, database=unreachable_db)
    client = BillingSpecialistClient(SPECIALIST_URL, transport=ASGITransport(app=app))

    result = await client.request_pricing_decision(
        account_id=uuid.uuid4(), target_plan_code="enterprise"
    )

    # The agent answered — it simply could not price this without a database.
    assert result.status is ConsultStatus.REFUSED
    assert result.recommendation is None


async def test_the_real_client_reads_the_real_agents_card(
    specialist_settings: Settings, unreachable_db: Database
) -> None:
    app = create_specialist(settings=specialist_settings, database=unreachable_db)
    client = BillingSpecialistClient(SPECIALIST_URL, transport=ASGITransport(app=app))

    card = await client.fetch_card()

    assert card is not None
    assert card["name"] == "billing-specialist"


# ------------------------------------------------------- the permission boundary


def test_the_specialist_holds_no_mutating_permission() -> None:
    """A2A adds a second opinion, not a second way to write.

    Every state change in this platform travels the MCP path with approval
    enforcement. If the specialist ever acquired a mutating tool it would become
    an unaudited mutation route that no approval gate covers.
    """
    for tool in tools_for_role(Role.BILLING_SPECIALIST):
        assert PERMISSION_MATRIX[tool].mutating is False


def test_the_specialist_cannot_read_customer_records() -> None:
    """The scope its verdict claims is the scope its role actually has.

    ``billing_eligible`` is named that way because this role cannot see customer
    or account standing. Granting ``get_customer`` here without renaming the
    field would let a partial verdict masquerade as a complete one.
    """
    permitted = tools_for_role(Role.BILLING_SPECIALIST)

    assert ToolName.GET_CUSTOMER not in permitted
    assert ToolName.GET_SUBSCRIPTION in permitted
    assert ToolName.GET_PRICING in permitted
    assert ToolName.GET_CONTRACT in permitted
    assert ToolName.GET_INVOICE in permitted


def test_the_reasoning_module_only_uses_tools_the_role_may_call() -> None:
    """Catch a widened reasoning path that the matrix would then reject at runtime."""
    import inspect

    from custops.apps.billing_specialist import reasoning

    source = inspect.getsource(reasoning)
    permitted = set(tools_for_role(Role.BILLING_SPECIALIST))

    for tool in ToolName:
        if f"ToolName.{tool.name}" in source:
            assert tool in permitted, f"{tool} is used but not permitted for the specialist"
