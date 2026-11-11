import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, update
from test_tools import auth_headers, insert_order, register

from app.core.celery import celery_app
from app.db.models import Order, OrderItem, OrganizationMembership, Product
from app.modules.agent.service import run_agent_workflow


@pytest.fixture
def order_chat(client, database_session_factory, monkeypatch):
    tenant = register(client, email="order-chat@example.com", slug="order-chat")
    organization_id = UUID(tenant["organization"]["id"])
    customer_id = insert_order(database_session_factory, organization_id, "CHAT")
    headers = auth_headers(tenant["access_token"])
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)

    def conversation(customer=True):
        result = client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"customer_id": str(customer_id)} if customer else {},
        )
        assert result.status_code == 201, result.text
        return result.json()["id"]

    def turn(conversation_id, text):
        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": text},
        )
        assert accepted.status_code == 202, accepted.text
        asyncio.run(
            run_agent_workflow(
                UUID(accepted.json()["id"]),
                database_session_factory,
                InMemorySaver(),
            )
        )
        workflow = client.get(
            f"/api/v1/workflows/{accepted.json()['id']}",
            headers=headers,
        ).json()
        assert workflow["status"] == "completed", workflow
        return client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
        ).json()["messages"][-1]["content"]

    return conversation, turn, organization_id, headers


def test_chat_order_quote_confirm_replay_and_orders_api(
    client,
    database_session_factory,
    order_chat,
):
    conversation, turn, organization_id, headers = order_chat
    chat = conversation()
    catalog = turn(chat, "list products")
    assert "SKU-CHAT" in catalog
    summary = turn(chat, "Buy 2 SKU-CHAT")
    assert "Customer CHAT" in summary
    assert "50.00 USD" in summary
    assert "No order has been created" in summary
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 1
    confirmation = "Confirm order."
    assert '"confirm order"' in summary
    assert not any(len(word) == 36 and word.count("-") == 4 for word in summary.split())
    # Another conversation cannot use this quote, even in the same tenant.
    assert "No matching order summary" in turn(conversation(), confirmation)
    created = turn(chat, confirmation)
    assert "Customer CHAT" in created
    assert "created" in created
    assert "No real payment was charged" in created
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 2
    assert turn(chat, confirmation) == created
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 2

    async def verify():
        async with database_session_factory() as session:
            product = await session.scalar(
                select(Product).where(
                    Product.organization_id == organization_id,
                    Product.sku == "SKU-CHAT",
                )
            )
            assert product.stock_quantity == 3
            assert await session.scalar(select(func.count()).select_from(OrderItem)) == 2
            order = await session.scalar(select(Order).where(Order.order_number.like("ORD-D-%")))
            assert order.total_amount == Decimal("50.00")
            assert order.status == "pending"

    asyncio.run(verify())


@pytest.mark.parametrize("change", ["price", "stock"])
def test_confirmation_revalidates_product(database_session_factory, order_chat, change):
    conversation, turn, organization_id, _ = order_chat
    chat = conversation()
    summary = turn(chat, "Buy 2 SKU-CHAT")
    assert '"confirm order"' in summary
    confirmation = "confirm order"

    async def modify():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(Product)
                .where(
                    Product.organization_id == organization_id,
                )
                .values(
                    **(
                        {"unit_price": Decimal("26.00")}
                        if change == "price"
                        else {"stock_quantity": 1}
                    )
                )
            )

    asyncio.run(modify())
    response = turn(chat, confirmation)
    assert ("price changed" if change == "price" else "no longer available") in response


@pytest.mark.parametrize("product", ["sku chat", "skuchat", "SKU - CHAT"])
def test_order_accepts_spoken_sku_spacing(order_chat, product):
    conversation, turn, _, _ = order_chat
    summary = turn(conversation(), f"Buy 2 {product}")
    assert "SKU-CHAT" in summary
    assert "50.00 USD" in summary
    assert "No order has been created" in summary


def test_normalized_sku_collision_is_rejected(database_session_factory, order_chat):
    conversation, turn, organization_id, _ = order_chat

    async def duplicate():
        async with database_session_factory() as session, session.begin():
            session.add(
                Product(
                    organization_id=organization_id,
                    sku="SKUCHAT",
                    name="Another product",
                    unit_price=Decimal("10.00"),
                    stock_quantity=10,
                )
            )

    asyncio.run(duplicate())
    assert "More than one product matches" in turn(conversation(), "Buy 2 sku chat")


@pytest.mark.parametrize("description", ["wireless headphones", "headphones"])
def test_order_accepts_unique_product_description(
    database_session_factory, order_chat, description
):
    conversation, turn, organization_id, _ = order_chat

    async def rename():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(Product).where(Product.organization_id == organization_id)
                .values(name="Studio Wireless Headphones")
            )

    asyncio.run(rename())
    summary = turn(conversation(), f"Buy 2 {description}")
    assert "SKU-CHAT" in summary
    assert "50.00 USD" in summary
    assert "No order has been created" in summary


def test_order_rejects_ambiguous_description(database_session_factory, order_chat):
    conversation, turn, organization_id, _ = order_chat

    async def products():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(Product).where(Product.organization_id == organization_id)
                .values(name="Studio Wireless Headphones")
            )
            session.add(Product(
                organization_id=organization_id, sku="OTHER-HEADPHONES",
                name="Travel Wireless Headphones", unit_price=Decimal("10.00"),
                stock_quantity=10,
            ))

    asyncio.run(products())
    assert "More than one product matches" in turn(conversation(), "Buy 2 headphones")


def test_quote_requires_customer_and_valid_quantity(order_chat):
    conversation, turn, _, _ = order_chat
    assert "customer attached" in turn(conversation(False), "Buy 2 SKU-CHAT")
    assert "No new order was confirmed" in turn(conversation(), "Buy 0 SKU-CHAT")


def test_confirmation_uses_latest_summary(client, order_chat):
    conversation, turn, _, headers = order_chat
    chat = conversation()
    turn(chat, "Buy 1 SKU-CHAT")
    turn(chat, "Buy 3 SKU-CHAT")
    response = turn(chat, "confirm order")
    assert "75.00 USD" in response
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 2


def test_failed_new_summary_does_not_confirm_old_summary(client, order_chat):
    conversation, turn, _, headers = order_chat
    chat = conversation()
    turn(chat, "Buy 1 SKU-CHAT")
    turn(chat, "Buy 2 NOT-A-PRODUCT")
    assert "No matching order summary" in turn(chat, "confirm order")
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 1


def test_expired_summary_is_not_confirmed(database_session_factory, order_chat):
    from datetime import UTC, datetime, timedelta

    from app.db.models import ToolExecution

    conversation, turn, organization_id, _ = order_chat
    chat = conversation()
    turn(chat, "Buy 1 SKU-CHAT")

    async def expire():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ToolExecution)
                .where(
                    ToolExecution.organization_id == organization_id,
                    ToolExecution.tool_name == "commerce_quote_demo_order",
                )
                .values(created_at=datetime.now(UTC) - timedelta(minutes=16))
            )

    asyncio.run(expire())
    assert "summary expired" in turn(chat, "confirm order")


def test_viewer_cannot_prepare_or_confirm_orders(database_session_factory, order_chat):
    conversation, turn, organization_id, _ = order_chat
    chat = conversation()
    summary = turn(chat, "Buy 2 SKU-CHAT")
    assert '"confirm order"' in summary
    confirmation = "confirm order"

    async def restrict():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == organization_id,
                )
                .values(role="viewer")
            )

    asyncio.run(restrict())
    assert "No new order was confirmed" in turn(chat, confirmation)
    assert "No new order was confirmed" in turn(chat, "Buy 1 SKU-CHAT")
