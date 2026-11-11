import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from mcp import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.celery import AGENT_WORKFLOW_TASK_NAME, celery_app
from app.db.models import (
    Customer,
    MembershipRole,
    Order,
    OrderItem,
    OrderStatus,
    OrganizationMembership,
    Product,
    Shipment,
    ShipmentStatus,
    SupportTicket,
    ToolExecution,
    WorkflowEvent,
)
from app.modules.agent.service import run_agent_workflow
from app.modules.tools.catalog import ToolExecutionContext, ToolFailure, tool_catalog
from app.modules.tools.mcp_server import build_mcp_server


def register(client: TestClient, *, email: str, slug: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "MCP Tool Test",
            "organization_slug": slug,
            "full_name": "Tool Test Admin",
            "email": email,
            "password": "Correct-Horse-Battery-1!",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def insert_order(
    session_factory: async_sessionmaker,
    organization_id: UUID,
    suffix: str,
) -> UUID:
    async def insert() -> UUID:
        async with session_factory() as session, session.begin():
            customer = Customer(
                organization_id=organization_id,
                email=f"customer-{suffix}@example.com",
                full_name=f"Customer {suffix}",
            )
            product = Product(
                organization_id=organization_id,
                sku=f"SKU-{suffix}",
                name=f"Product {suffix}",
                description="Tool test product",
                unit_price=Decimal("25.00"),
                stock_quantity=5,
            )
            session.add_all([customer, product])
            await session.flush()
            order = Order(
                organization_id=organization_id,
                customer_id=customer.id,
                order_number=f"ORD-{suffix}",
                status=OrderStatus.DELAYED.value,
                total_amount=Decimal("25.00"),
                currency="USD",
            )
            session.add(order)
            await session.flush()
            session.add_all(
                [
                    OrderItem(
                        organization_id=organization_id,
                        order_id=order.id,
                        product_id=product.id,
                        quantity=1,
                        unit_price=Decimal("25.00"),
                    ),
                    Shipment(
                        organization_id=organization_id,
                        order_id=order.id,
                        tracking_number=f"TRACK-{suffix}",
                        carrier=f"Carrier {suffix}",
                        status=ShipmentStatus.DELAYED.value,
                    ),
                ]
            )
            return customer.id

    return asyncio.run(insert())


def test_mcp_server_lists_typed_tools_by_role(database_session_factory) -> None:
    async def list_tools(role: MembershipRole) -> list:
        context = ToolExecutionContext(
            organization_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            actor_user_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            role=role,
            workflow_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            conversation_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            customer_id=None,
            session_factory=database_session_factory,
        )
        async with Client(build_mcp_server(context)) as client:
            return (await client.list_tools()).tools

    viewer_tools = asyncio.run(list_tools(MembershipRole.VIEWER))
    assert {tool.name for tool in viewer_tools} == {
        "commerce_list_products",
        "commerce_get_order_status",
    }
    order_tool = next(tool for tool in viewer_tools if tool.name == "commerce_get_order_status")
    order_schema = order_tool.input_schema
    assert order_schema["properties"]["order_number"]["type"] == "string"
    assert order_schema["required"] == ["order_number"]
    assert order_tool.annotations.read_only_hint is True
    assert order_tool.meta["resolveai/category"] == "commerce"

    admin_tools = asyncio.run(list_tools(MembershipRole.ADMIN))
    assert {tool.name for tool in admin_tools} == {
        "commerce_get_order_status",
        "commerce_get_refund_status",
        "support_create_ticket",
        "commerce_list_products",
        "commerce_quote_demo_order",
        "commerce_confirm_demo_order",
        "commerce_request_action",
    }
    write_tool = next(tool for tool in admin_tools if tool.name == "support_create_ticket")
    assert write_tool.annotations.read_only_hint is False
    assert write_tool.annotations.idempotent_hint is True
    assert write_tool.meta["resolveai/operation"] == "write"


def test_order_tool_is_tenant_scoped_and_audited(
    client: TestClient,
    database_session_factory: async_sessionmaker,
    monkeypatch,
) -> None:
    tenant_a = register(client, email="tools-a@example.com", slug="tools-a")
    tenant_b = register(client, email="tools-b@example.com", slug="tools-b")
    customer_a = insert_order(
        database_session_factory,
        UUID(tenant_a["organization"]["id"]),
        "A",
    )
    insert_order(
        database_session_factory,
        UUID(tenant_b["organization"]["id"]),
        "B",
    )
    queued: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, args: queued.append((name, args)))
    headers = auth_headers(tenant_a["access_token"])
    conversation = client.post(
        "/api/v1/conversations",
        headers=headers,
        json={"customer_id": str(customer_a)},
    ).json()

    async def run_workflow(workflow_id: str) -> None:
        await run_agent_workflow(UUID(workflow_id), database_session_factory, InMemorySaver())

    foreign_order_turn = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": "Where is my order ORD-B?"},
    )
    assert foreign_order_turn.status_code == 202, foreign_order_turn.text
    asyncio.run(run_workflow(foreign_order_turn.json()["id"]))
    foreign_workflow = client.get(
        f"/api/v1/workflows/{foreign_order_turn.json()['id']}",
        headers=headers,
    ).json()
    assert "tool.failed" in [event["event_type"] for event in foreign_workflow["events"]]
    assistant_reply = client.get(
        f"/api/v1/conversations/{conversation['id']}",
        headers=headers,
    ).json()["messages"][-1]["content"]
    assert "Carrier B" not in assistant_reply
    assert "I couldn't retrieve" in assistant_reply

    own_order_turn = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": "Where is my order ORD-A?"},
    )
    assert own_order_turn.status_code == 202, own_order_turn.text
    asyncio.run(run_workflow(own_order_turn.json()["id"]))
    assistant_reply = client.get(
        f"/api/v1/conversations/{conversation['id']}",
        headers=headers,
    ).json()["messages"][-1]["content"]
    assert "Order ORD-A is currently delayed." in assistant_reply
    assert "Carrier A shipment is delayed." in assistant_reply
    assert queued == [
        (AGENT_WORKFLOW_TASK_NAME, [foreign_order_turn.json()["id"]]),
        (AGENT_WORKFLOW_TASK_NAME, [own_order_turn.json()["id"]]),
    ]

    async def verify_audit() -> None:
        async with database_session_factory() as session:
            executions = list(
                (
                    await session.scalars(
                        select(ToolExecution)
                        .where(
                            ToolExecution.organization_id == UUID(tenant_a["organization"]["id"])
                        )
                        .order_by(ToolExecution.created_at, ToolExecution.id)
                    )
                ).all()
            )
            assert [execution.status for execution in executions] == ["failed", "succeeded"]
            assert executions[0].error_code == "order_not_found"
            assert executions[1].tool_name == "commerce_get_order_status"
            assert executions[1].actor_user_id == UUID(tenant_a["user"]["id"])
            assert executions[1].result_payload is None
            events = list(
                (
                    await session.scalars(
                        select(WorkflowEvent).where(
                            WorkflowEvent.workflow_id == UUID(own_order_turn.json()["id"])
                        )
                    )
                ).all()
            )
            assert any(event.event_type == "tool.completed" for event in events)

    asyncio.run(verify_audit())


def test_support_ticket_tool_is_idempotent_and_customer_scoped(
    client: TestClient,
    database_session_factory: async_sessionmaker,
    monkeypatch,
) -> None:
    tenant = register(client, email="ticket-tools@example.com", slug="ticket-tools")
    customer_id = insert_order(
        database_session_factory,
        UUID(tenant["organization"]["id"]),
        "TICKET",
    )
    queued: list[str] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args: queued.append(args[0]) if name == AGENT_WORKFLOW_TASK_NAME else None,
    )
    headers = auth_headers(tenant["access_token"])
    conversation = client.post(
        "/api/v1/conversations",
        headers=headers,
        json={"customer_id": str(customer_id)},
    ).json()
    submitted = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": "Please create a support ticket for this issue."},
    )
    assert submitted.status_code == 202, submitted.text
    workflow_id = UUID(submitted.json()["id"])

    asyncio.run(run_agent_workflow(workflow_id, database_session_factory, InMemorySaver()))
    reply = client.get(
        f"/api/v1/conversations/{conversation['id']}",
        headers=headers,
    ).json()["messages"][-1]["content"]
    assert "I've created support ticket TKT-" in reply

    async def replay_ticket() -> list[dict]:
        context = ToolExecutionContext(
            organization_id=UUID(tenant["organization"]["id"]),
            actor_user_id=UUID(tenant["user"]["id"]),
            role=MembershipRole.ADMIN,
            workflow_id=workflow_id,
            conversation_id=UUID(conversation["id"]),
            customer_id=customer_id,
            session_factory=database_session_factory,
        )
        async with Client(build_mcp_server(context)) as tool_client:
            results = [
                await tool_client.call_tool(
                    "support_create_ticket",
                    {
                        "subject": "Different title on replay",
                        "description": "This retry must not create a second ticket.",
                    },
                )
                for _ in range(2)
            ]
        return [result.structured_content for result in results]

    replayed = asyncio.run(replay_ticket())
    assert replayed[0] == replayed[1]
    assert replayed[0]["ticket_number"] in reply
    assert queued == [str(workflow_id)]

    async def verify_idempotency() -> None:
        async with database_session_factory() as session:
            tickets = list(
                (
                    await session.scalars(
                        select(SupportTicket).where(
                            SupportTicket.organization_id == UUID(tenant["organization"]["id"]),
                            SupportTicket.customer_id == customer_id,
                        )
                    )
                ).all()
            )
            executions = list(
                (
                    await session.scalars(
                        select(ToolExecution).where(
                            ToolExecution.workflow_id == workflow_id,
                            ToolExecution.tool_name == "support_create_ticket",
                        )
                    )
                ).all()
            )
            assert len(tickets) == 1
            assert len(executions) == 1
            assert executions[0].status == "succeeded"
            assert executions[0].idempotency_key == (f"{workflow_id}:support_create_ticket")

    asyncio.run(verify_idempotency())


def test_viewer_role_cannot_run_support_ticket_write(
    client: TestClient,
    database_session_factory: async_sessionmaker,
    monkeypatch,
) -> None:
    tenant = register(client, email="viewer-tools@example.com", slug="viewer-tools")
    customer_id = insert_order(
        database_session_factory,
        UUID(tenant["organization"]["id"]),
        "VIEWER",
    )
    monkeypatch.setattr(celery_app, "send_task", lambda _name, args: None)
    headers = auth_headers(tenant["access_token"])
    conversation = client.post(
        "/api/v1/conversations",
        headers=headers,
        json={"customer_id": str(customer_id)},
    ).json()
    submitted = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": "Please create a support ticket for this issue."},
    )
    assert submitted.status_code == 202, submitted.text
    workflow_id = UUID(submitted.json()["id"])
    organization_id = UUID(tenant["organization"]["id"])
    actor_user_id = UUID(tenant["user"]["id"])
    conversation_id = UUID(conversation["id"])

    async def set_viewer_role() -> None:
        async with database_session_factory() as session, session.begin():
            membership = await session.scalar(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == actor_user_id,
                )
            )
            assert membership is not None
            membership.role = MembershipRole.VIEWER.value

    asyncio.run(set_viewer_role())
    asyncio.run(run_agent_workflow(workflow_id, database_session_factory, InMemorySaver()))

    response = client.get(
        f"/api/v1/conversations/{conversation['id']}",
        headers=headers,
    ).json()["messages"][-1]["content"]
    assert "can't create a ticket with the current role" in response

    context = ToolExecutionContext(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        role=MembershipRole.VIEWER,
        workflow_id=workflow_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        session_factory=database_session_factory,
    )

    async def invoke_denied_tool() -> None:
        with pytest.raises(ToolFailure, match="current role cannot use this tool"):
            await tool_catalog.invoke(
                "support_create_ticket",
                {"subject": "Need help", "description": "This write must be denied."},
                context,
            )

    asyncio.run(invoke_denied_tool())

    async def verify_denial() -> None:
        async with database_session_factory() as session:
            tickets = list(
                (
                    await session.scalars(
                        select(SupportTicket).where(
                            SupportTicket.organization_id == organization_id,
                            SupportTicket.customer_id == customer_id,
                        )
                    )
                ).all()
            )
            execution = await session.scalar(
                select(ToolExecution).where(
                    ToolExecution.workflow_id == workflow_id,
                    ToolExecution.tool_name == "support_create_ticket",
                    ToolExecution.status == "failed",
                )
            )
            assert tickets == []
            assert execution is not None
            assert execution.error_code == "permission_denied"

    asyncio.run(verify_denial())
