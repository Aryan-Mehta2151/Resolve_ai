import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.models import (
    Customer,
    Order,
    OrderItem,
    OrderStatus,
    Organization,
    Payment,
    PaymentStatus,
    Product,
    Shipment,
    ShipmentStatus,
    SupportTicket,
    SupportTicketPriority,
    SupportTicketStatus,
)

_PRODUCTS = (
    {
        "sku": "AUDIO-100",
        "name": "Studio Wireless Headphones",
        "description": "Over-ear Bluetooth headphones with active noise cancellation.",
        "unit_price": Decimal("129.99"),
        "stock_quantity": 42,
    },
    {
        "sku": "AUDIO-101",
        "name": "USB-C Audio Cable",
        "description": "Durable one-meter USB-C charging and audio cable.",
        "unit_price": Decimal("19.99"),
        "stock_quantity": 120,
    },
)


async def seed_demo_data(organization_slug: str) -> dict[str, int]:
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    counts = {
        "customers": 0,
        "products": 0,
        "orders": 0,
        "shipments": 0,
        "payments": 0,
        "tickets": 0,
    }
    try:
        async with session_factory() as session, session.begin():
            organization = await session.scalar(
                select(Organization).where(
                    Organization.slug == organization_slug,
                    Organization.is_active.is_(True),
                )
            )
            if organization is None:
                raise ValueError(
                    f"Active organization '{organization_slug}' does not exist; create it first."
                )

            customer = await session.scalar(
                select(Customer).where(
                    Customer.organization_id == organization.id,
                    Customer.email == "jamie.rivera@example.com",
                )
            )
            if customer is None:
                customer = Customer(
                    organization_id=organization.id,
                    email="jamie.rivera@example.com",
                    full_name="Jamie Rivera",
                    phone_number="+15550100",
                    phone_number_normalized="+15550100",
                )
                session.add(customer)
                await session.flush()
                counts["customers"] += 1

            products: dict[str, Product] = {}
            for values in _PRODUCTS:
                product = await session.scalar(
                    select(Product).where(
                        Product.organization_id == organization.id,
                        Product.sku == values["sku"],
                    )
                )
                if product is None:
                    product = Product(organization_id=organization.id, **values)
                    session.add(product)
                    await session.flush()
                    counts["products"] += 1
                products[product.sku] = product

            order = await session.scalar(
                select(Order).where(
                    Order.organization_id == organization.id,
                    Order.order_number == "ORD-10045",
                )
            )
            if order is None:
                order = Order(
                    organization_id=organization.id,
                    customer_id=customer.id,
                    order_number="ORD-10045",
                    status=OrderStatus.DELAYED.value,
                    total_amount=Decimal("129.99"),
                    currency="USD",
                )
                session.add(order)
                await session.flush()
                session.add(
                    OrderItem(
                        organization_id=organization.id,
                        order_id=order.id,
                        product_id=products["AUDIO-100"].id,
                        quantity=1,
                        unit_price=Decimal("129.99"),
                    )
                )
                counts["orders"] += 1

            shipment = await session.scalar(
                select(Shipment).where(
                    Shipment.organization_id == organization.id,
                    Shipment.tracking_number == "MOCK-TRACK-10045",
                )
            )
            if shipment is None:
                shipped_at = datetime.now(UTC) - timedelta(days=2)
                session.add(
                    Shipment(
                        organization_id=organization.id,
                        order_id=order.id,
                        tracking_number="MOCK-TRACK-10045",
                        carrier="Northstar Parcel",
                        status=ShipmentStatus.DELAYED.value,
                        shipped_at=shipped_at,
                        estimated_delivery_at=datetime.now(UTC) + timedelta(days=3),
                    )
                )
                counts["shipments"] += 1

            payment = await session.scalar(
                select(Payment).where(
                    Payment.organization_id == organization.id,
                    Payment.provider_reference == "MOCK-PAYMENT-ORD-10045",
                )
            )
            if payment is None:
                session.add(
                    Payment(
                        organization_id=organization.id,
                        order_id=order.id,
                        provider="mock",
                        provider_reference="MOCK-PAYMENT-ORD-10045",
                        status=PaymentStatus.SUCCEEDED.value,
                        amount=Decimal("129.99"),
                        currency="USD",
                    )
                )
                counts["payments"] += 1

            ticket = await session.scalar(
                select(SupportTicket).where(
                    SupportTicket.organization_id == organization.id,
                    SupportTicket.ticket_number == "TKT-DEMO-10045",
                )
            )
            if ticket is None:
                session.add(
                    SupportTicket(
                        organization_id=organization.id,
                        customer_id=customer.id,
                        order_id=order.id,
                        ticket_number="TKT-DEMO-10045",
                        subject="Delayed delivery for ORD-10045",
                        description="Customer contacted support about a delayed delivery.",
                        status=SupportTicketStatus.OPEN.value,
                        priority=SupportTicketPriority.NORMAL.value,
                    )
                )
                counts["tickets"] += 1
        return counts
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed mock commerce data for one tenant.")
    parser.add_argument(
        "--organization-slug",
        required=True,
        help="Slug of an existing organization to receive the demo data.",
    )
    arguments = parser.parse_args()
    counts = asyncio.run(seed_demo_data(arguments.organization_slug))
    print(f"Seeded new records: {counts}")


if __name__ == "__main__":
    main()
