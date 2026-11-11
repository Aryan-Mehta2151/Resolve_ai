import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Customer,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Product,
    Refund,
    RefundStatus,
    Shipment,
    ShipmentStatus,
)


def register(
    client: TestClient,
    *,
    email: str,
    slug: str,
) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Commerce Test",
            "organization_slug": slug,
            "full_name": "Commerce Admin",
            "email": email,
            "password": "Correct-Horse-Battery-1!",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def insert_commerce_data(
    session_factory: async_sessionmaker,
    organization_id: UUID,
    suffix: str,
) -> dict[str, UUID]:
    async with session_factory() as session, session.begin():
        customer = Customer(
            organization_id=organization_id,
            email=f"jamie-{suffix}@example.com",
            full_name=f"Jamie {suffix}",
            phone_number="+1-555-0100",
        )
        product = Product(
            organization_id=organization_id,
            sku=f"AUDIO-{suffix}",
            name=f"Wireless Headphones {suffix}",
            description="Mock product",
            unit_price=Decimal("129.99"),
            stock_quantity=42,
        )
        session.add_all([customer, product])
        await session.flush()

        order = Order(
            organization_id=organization_id,
            customer_id=customer.id,
            order_number=f"ORD-{suffix}",
            status=OrderStatus.DELAYED.value,
            total_amount=Decimal("129.99"),
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
                    unit_price=Decimal("129.99"),
                ),
                Shipment(
                    organization_id=organization_id,
                    order_id=order.id,
                    tracking_number=f"TRACK-{suffix}",
                    carrier="Northstar Parcel",
                    status=ShipmentStatus.DELAYED.value,
                ),
            ]
        )

        payment = Payment(
            organization_id=organization_id,
            order_id=order.id,
            provider="mock",
            provider_reference=f"PAYMENT-{suffix}",
            status=PaymentStatus.SUCCEEDED.value,
            amount=Decimal("129.99"),
            currency="USD",
        )
        session.add(payment)
        await session.flush()
        refund = Refund(
            organization_id=organization_id,
            payment_id=payment.id,
            idempotency_key=f"REFUND-{suffix}",
            status=RefundStatus.PENDING.value,
            amount=Decimal("25.00"),
            reason="Test refund",
        )
        session.add(refund)
        await session.flush()

        return {
            "customer_id": customer.id,
            "product_id": product.id,
            "order_id": order.id,
            "payment_id": payment.id,
            "refund_id": refund.id,
        }


def create_commerce_data(session_factory, organization_id: str, suffix: str) -> dict[str, UUID]:
    return asyncio.run(insert_commerce_data(session_factory, UUID(organization_id), suffix))


def test_commerce_reads_are_tenant_scoped_and_include_order_details(
    client: TestClient,
    database_session_factory: async_sessionmaker,
) -> None:
    tenant_a = register(client, email="admin-a@example.com", slug="commerce-a")
    tenant_b = register(client, email="admin-b@example.com", slug="commerce-b")
    data_a = create_commerce_data(database_session_factory, tenant_a["organization"]["id"], "A")
    data_b = create_commerce_data(database_session_factory, tenant_b["organization"]["id"], "B")
    headers = auth_headers(tenant_a["access_token"])

    customers = client.get("/api/v1/customers", headers=headers)
    assert customers.status_code == 200
    assert [customer["full_name"] for customer in customers.json()] == ["Jamie A"]
    assert (
        client.get(f"/api/v1/customers/{data_b['customer_id']}", headers=headers).status_code == 404
    )

    products = client.get("/api/v1/products?search=Wireless", headers=headers)
    assert products.status_code == 200
    assert [product["sku"] for product in products.json()] == ["AUDIO-A"]
    assert (
        client.get(f"/api/v1/products/{data_b['product_id']}", headers=headers).status_code == 404
    )

    order = client.get("/api/v1/orders/by-number/ORD-A", headers=headers)
    assert order.status_code == 200
    assert order.json()["id"] == str(data_a["order_id"])
    assert order.json()["items"][0]["unit_price"] == "129.99"
    assert client.get(f"/api/v1/orders/{data_b['order_id']}", headers=headers).status_code == 404

    shipments = client.get(
        f"/api/v1/orders/{data_a['order_id']}/shipments",
        headers=headers,
    )
    assert shipments.status_code == 200
    assert shipments.json()[0]["tracking_number"] == "TRACK-A"

    payments = client.get(
        f"/api/v1/orders/{data_a['order_id']}/payments",
        headers=headers,
    )
    assert payments.status_code == 200
    assert payments.json()[0]["refunds"][0]["id"] == str(data_a["refund_id"])

    refunds = client.get(
        f"/api/v1/orders/{data_a['order_id']}/refunds",
        headers=headers,
    )
    assert refunds.status_code == 200
    assert refunds.json()[0]["payment_id"] == str(data_a["payment_id"])
    assert (
        client.get(f"/api/v1/orders/{data_b['order_id']}/payments", headers=headers).status_code
        == 404
    )


def test_support_ticket_creation_validates_tenant_customer_order_and_role(
    client: TestClient,
    database_session_factory: async_sessionmaker,
) -> None:
    tenant_a = register(client, email="ticket-a@example.com", slug="ticket-a")
    tenant_b = register(client, email="ticket-b@example.com", slug="ticket-b")
    data_a = create_commerce_data(database_session_factory, tenant_a["organization"]["id"], "A")
    data_b = create_commerce_data(database_session_factory, tenant_b["organization"]["id"], "B")
    headers = auth_headers(tenant_a["access_token"])
    payload = {
        "customer_id": str(data_a["customer_id"]),
        "order_id": str(data_a["order_id"]),
        "subject": "  Delivery update needed  ",
        "description": "Customer asks for a delivery update.",
        "priority": "high",
    }

    created = client.post("/api/v1/support/tickets", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["subject"] == "Delivery update needed"
    assert created.json()["status"] == "open"
    assert (
        client.get(
            f"/api/v1/support/tickets/{created.json()['id']}",
            headers=headers,
        ).status_code
        == 200
    )
    assert len(client.get("/api/v1/support/tickets", headers=headers).json()) == 1

    foreign_customer = {**payload, "customer_id": str(data_b["customer_id"])}
    assert (
        client.post("/api/v1/support/tickets", headers=headers, json=foreign_customer).status_code
        == 404
    )
    foreign_order = {**payload, "order_id": str(data_b["order_id"])}
    assert (
        client.post("/api/v1/support/tickets", headers=headers, json=foreign_order).status_code
        == 404
    )
    invalid_subject = {**payload, "subject": "  "}
    assert (
        client.post("/api/v1/support/tickets", headers=headers, json=invalid_subject).status_code
        == 422
    )

    organization_id = tenant_a["organization"]["id"]
    member = client.post(
        f"/api/v1/organizations/{organization_id}/memberships",
        headers=headers,
        json={
            "full_name": "Read Only",
            "email": "read-only@example.com",
            "password": "Correct-Horse-Battery-2!",
            "role": "viewer",
        },
    )
    assert member.status_code == 201, member.text
    viewer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "read-only@example.com", "password": "Correct-Horse-Battery-2!"},
    )
    assert viewer_login.status_code == 200
    denied = client.post(
        "/api/v1/support/tickets",
        headers=auth_headers(viewer_login.json()["access_token"]),
        json=payload,
    )
    assert denied.status_code == 403


def test_composite_foreign_keys_reject_cross_tenant_order_items(
    client: TestClient,
    database_session_factory: async_sessionmaker,
) -> None:
    tenant_a = register(client, email="fk-a@example.com", slug="fk-a")
    tenant_b = register(client, email="fk-b@example.com", slug="fk-b")
    data_a = create_commerce_data(database_session_factory, tenant_a["organization"]["id"], "A")
    data_b = create_commerce_data(database_session_factory, tenant_b["organization"]["id"], "B")
    organization_a = UUID(tenant_a["organization"]["id"])

    async def insert_cross_tenant_item() -> None:
        async with database_session_factory() as session:
            session.add(
                OrderItem(
                    organization_id=organization_a,
                    order_id=data_a["order_id"],
                    product_id=data_b["product_id"],
                    quantity=1,
                    unit_price=Decimal("129.99"),
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

    asyncio.run(insert_cross_tenant_item())
