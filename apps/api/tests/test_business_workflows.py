import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select, update
from test_tools import auth_headers, insert_order, register

from app.core.celery import celery_app
from app.core.config import settings
from app.db.models import (
    Order,
    OrganizationMembership,
    Payment,
    Product,
    Refund,
    Shipment,
)
from app.modules.agent.service import run_agent_workflow


@pytest.fixture
def commerce_runtime(client, database_session_factory, monkeypatch):
    monkeypatch.setattr(settings, "demo_business_workflows_enabled", True)
    tenant = register(client, email="business@example.com", slug="business")
    org = UUID(tenant["organization"]["id"])
    customer = insert_order(database_session_factory, org, "BUSINESS")
    headers = auth_headers(tenant["access_token"])

    async def setup():
        async with database_session_factory() as session, session.begin():
            order = await session.scalar(select(Order).where(Order.organization_id == org))
            session.add(
                Payment(
                    organization_id=org,
                    order_id=order.id,
                    provider="mock",
                    provider_reference="MOCK-BUSINESS",
                    status="succeeded",
                    amount=Decimal("25.00"),
                    currency="USD",
                )
            )
            return str(order.id)

    order_id = asyncio.run(setup())
    return org, headers, order_id, customer


def action(client, runtime, kind="refund", confirmed=True):
    _, headers, order_id, _ = runtime
    return client.post(
        f"/api/v1/orders/{order_id}/actions",
        headers=headers,
        json={
            "kind": kind,
            "reason": "Customer requested assistance",
            "confirmed": confirmed,
        },
    )


def test_refund_approval_and_replay(client, database_session_factory, commerce_runtime):
    org, headers, _, _ = commerce_runtime
    requested = action(client, commerce_runtime)
    assert requested.status_code == 200, requested.text
    assert requested.json()["status"] == "pending"
    assert action(client, commerce_runtime).json()["id"] == requested.json()["id"]
    url = f"/api/v1/business-actions/{requested.json()['id']}/decision"
    approved = client.post(url, headers=headers, json={"approve": True, "note": "Mock verified"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "succeeded"
    assert approved.json()["reviewer_id"] is not None
    assert (
        client.post(
            url,
            headers=headers,
            json={
                "approve": True,
                "note": "Repeat decision",
            },
        ).json()["decision_note"]
        == "Mock verified"
    )
    assert (
        client.post(
            url,
            headers=headers,
            json={
                "approve": False,
                "note": "Conflicting decision",
            },
        ).status_code
        == 409
    )

    async def verify():
        async with database_session_factory() as session:
            refunds = list(
                await session.scalars(select(Refund).where(Refund.organization_id == org))
            )
            assert len(refunds) == 1
            assert refunds[0].amount == Decimal("25.00")
            assert refunds[0].status == "succeeded"
            payment = await session.scalar(select(Payment).where(Payment.organization_id == org))
            assert payment.status == "refunded"

    asyncio.run(verify())


def test_refund_rejection(client, commerce_runtime):
    _, headers, _, _ = commerce_runtime
    requested = action(client, commerce_runtime).json()
    response = client.post(
        f"/api/v1/business-actions/{requested['id']}/decision",
        headers=headers,
        json={"approve": False, "note": "Not eligible for demo"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_confirmation_feature_flag_and_tenant_boundaries(
    client,
    commerce_runtime,
    monkeypatch,
):
    assert action(client, commerce_runtime, confirmed=False).status_code == 422
    foreign = register(client, email="foreign-business@example.com", slug="foreign-business")
    foreign_headers = auth_headers(foreign["access_token"])
    _, headers, order_id, _ = commerce_runtime
    assert (
        client.post(
            f"/api/v1/orders/{order_id}/actions",
            headers=foreign_headers,
            json={
                "kind": "refund",
                "reason": "Other tenant",
                "confirmed": True,
            },
        ).status_code
        == 404
    )
    requested = action(client, commerce_runtime).json()
    assert client.get("/api/v1/business-actions", headers=foreign_headers).json() == []
    assert (
        client.post(
            f"/api/v1/business-actions/{requested['id']}/decision",
            headers=foreign_headers,
            json={"approve": True, "note": "Other tenant"},
        ).status_code
        == 404
    )
    monkeypatch.setattr(settings, "demo_business_workflows_enabled", False)
    assert action(client, commerce_runtime).status_code == 503
    assert (
        client.post(
            f"/api/v1/business-actions/{requested['id']}/decision",
            headers=headers,
            json={"approve": True, "note": "Disabled workflows"},
        ).status_code
        == 503
    )


@pytest.mark.parametrize("role", ["viewer", "support_agent"])
def test_workflow_roles(client, database_session_factory, commerce_runtime, role):
    org, headers, _, _ = commerce_runtime
    requested = action(client, commerce_runtime).json()

    async def restrict():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == org,
                )
                .values(role=role)
            )

    asyncio.run(restrict())
    assert (
        client.post(
            f"/api/v1/business-actions/{requested['id']}/decision",
            headers=headers,
            json={"approve": True, "note": "Cannot approve"},
        ).status_code
        == 403
    )
    assert action(client, commerce_runtime).status_code == (403 if role == "viewer" else 200)


def test_cancellation_restores_stock_once(client, database_session_factory, commerce_runtime):
    org, headers, order_id, _ = commerce_runtime

    async def prepare():
        async with database_session_factory() as session, session.begin():
            await session.execute(delete(Payment).where(Payment.organization_id == org))
            await session.execute(delete(Shipment).where(Shipment.organization_id == org))
            await session.execute(
                update(Order)
                .where(Order.organization_id == org)
                .values(
                    order_number="ORD-D-BUSINESS",
                    status="pending",
                )
            )

    asyncio.run(prepare())
    first = action(client, commerce_runtime, "cancellation")
    assert first.status_code == 200, first.text
    assert action(client, commerce_runtime, "cancellation").json()["id"] == first.json()["id"]
    assert client.get(f"/api/v1/orders/{order_id}", headers=headers).json()["status"] == "cancelled"

    async def verify():
        async with database_session_factory() as session:
            product = await session.scalar(select(Product).where(Product.organization_id == org))
            assert product.stock_quantity == 6

    asyncio.run(verify())


@pytest.mark.parametrize("condition", ["shipped", "paid", "real_provider"])
def test_policy_denials(client, database_session_factory, commerce_runtime, condition):
    org, _, _, _ = commerce_runtime

    async def modify():
        async with database_session_factory() as session, session.begin():
            if condition == "shipped":
                await session.execute(
                    update(Order)
                    .where(Order.organization_id == org)
                    .values(
                        status="shipped",
                    )
                )
            elif condition == "real_provider":
                await session.execute(
                    update(Payment)
                    .where(Payment.organization_id == org)
                    .values(
                        provider="live-provider",
                    )
                )

    asyncio.run(modify())
    response = action(
        client, commerce_runtime, "refund" if condition == "real_provider" else "cancellation"
    )
    assert response.status_code == 409


def test_chat_refund_and_escalation_review(
    client,
    database_session_factory,
    commerce_runtime,
    monkeypatch,
):
    _, headers, _, customer = commerce_runtime
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)
    conversation = client.post(
        "/api/v1/conversations",
        headers=headers,
        json={
            "customer_id": str(customer),
        },
    ).json()

    def turn(text):
        response = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"content": text},
        )
        assert response.status_code == 202, response.text
        asyncio.run(
            run_agent_workflow(
                UUID(response.json()["id"]),
                database_session_factory,
                InMemorySaver(),
            )
        )
        return client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers).json()

    reply = turn("request refund ORD-BUSINESS")
    assert "awaiting Admin/Supervisor approval" in reply["messages"][-1]["content"]
    assert client.get("/api/v1/business-actions", headers=headers).json()[0]["status"] == "pending"
    escalated = turn("Please let me speak to a human agent")
    assert escalated["status"] == "escalated"
    queue = client.get("/api/v1/conversations?status=escalated", headers=headers)
    assert len(queue.json()) == 1
    reviewed = client.post(
        f"/api/v1/conversations/{conversation['id']}/review",
        headers=headers,
        json={"status": "open", "note": "Reviewed and returned to assistant"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "open"
    assert client.get("/api/v1/conversations?status=escalated", headers=headers).json() == []
    assert turn("Hello!")["messages"][-1]["role"] == "assistant"
