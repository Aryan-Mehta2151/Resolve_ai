from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, SessionDep, require_roles
from app.db.models import (
    Customer,
    MembershipRole,
    Order,
    OrderItem,
    Payment,
    Product,
    Refund,
    Shipment,
    SupportTicket,
    SupportTicketStatus,
)
from app.modules.commerce.schemas import (
    CustomerCreateRequest,
    CustomerResponse,
    OrderItemResponse,
    OrderResponse,
    PaymentResponse,
    ProductResponse,
    RefundResponse,
    ShipmentResponse,
    SupportTicketCreate,
    SupportTicketResponse,
)

router = APIRouter(prefix="/api/v1", tags=["commerce"])
SupportAgent = Annotated[
    CurrentUser,
    Depends(
        require_roles(
            MembershipRole.ADMIN,
            MembershipRole.SUPERVISOR,
            MembershipRole.SUPPORT_AGENT,
        )
    ),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


async def _order_response(
    order: Order,
    organization_id: UUID,
    session: AsyncSession,
) -> OrderResponse:
    items = await session.scalars(
        select(OrderItem)
        .where(
            OrderItem.organization_id == organization_id,
            OrderItem.order_id == order.id,
        )
        .order_by(OrderItem.id)
    )
    return OrderResponse(
        id=order.id,
        customer_id=order.customer_id,
        order_number=order.order_number,
        status=order.status,
        total_amount=order.total_amount,
        currency=order.currency,
        created_at=order.created_at,
        items=[OrderItemResponse.model_validate(item) for item in items],
    )


@router.get("/customers", response_model=list[CustomerResponse])
async def list_customers(
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[CustomerResponse]:
    customers = await session.scalars(
        select(Customer)
        .where(Customer.organization_id == user.organization_id)
        .order_by(Customer.full_name, Customer.id)
        .limit(limit)
        .offset(offset)
    )
    return [CustomerResponse.model_validate(customer) for customer in customers]


@router.post("/customers", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CustomerCreateRequest,
    user: SupportAgent,
    session: SessionDep,
) -> CustomerResponse:
    email = str(payload.email).strip().lower()
    existing = await session.scalar(
        select(Customer.id).where(
            Customer.organization_id == user.organization_id,
            func.lower(Customer.email) == email,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A customer with this email already exists")
    customer = Customer(
        organization_id=user.organization_id,
        email=email,
        full_name=payload.full_name,
        phone_number=payload.phone_number,
        phone_number_normalized=payload.phone_number,
    )
    session.add(customer)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A customer with this email already exists",
        ) from exc
    await session.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.get("/customers/{customer_id}", response_model=CustomerResponse)
async def get_customer(
    customer_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> CustomerResponse:
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.organization_id == user.organization_id,
        )
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return CustomerResponse.model_validate(customer)


@router.get("/customers/{customer_id}/orders", response_model=list[OrderResponse])
async def list_customer_orders(
    customer_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[OrderResponse]:
    customer_exists = await session.scalar(
        select(Customer.id).where(
            Customer.id == customer_id,
            Customer.organization_id == user.organization_id,
        )
    )
    if customer_exists is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    orders = await session.scalars(
        select(Order)
        .where(
            Order.customer_id == customer_id,
            Order.organization_id == user.organization_id,
        )
        .order_by(Order.created_at.desc(), Order.id)
        .limit(limit)
        .offset(offset)
    )
    return [await _order_response(order, user.organization_id, session) for order in orders]


@router.get("/products", response_model=list[ProductResponse])
async def list_products(
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
    search: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> list[ProductResponse]:
    statement = select(Product).where(
        Product.organization_id == user.organization_id,
        Product.is_active.is_(True),
    )
    if search is not None:
        statement = statement.where(
            Product.name.ilike(f"%{search.strip()}%") | Product.sku.ilike(f"%{search.strip()}%")
        )
    products = await session.scalars(
        statement.order_by(Product.name, Product.id).limit(limit).offset(offset)
    )
    return [ProductResponse.model_validate(product) for product in products]


@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> ProductResponse:
    product = await session.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.organization_id == user.organization_id,
            Product.is_active.is_(True),
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductResponse.model_validate(product)


@router.get("/orders", response_model=list[OrderResponse])
async def list_orders(
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[OrderResponse]:
    orders = await session.scalars(
        select(Order)
        .where(Order.organization_id == user.organization_id)
        .order_by(Order.created_at.desc(), Order.id)
        .limit(limit)
        .offset(offset)
    )
    return [await _order_response(order, user.organization_id, session) for order in orders]


@router.get("/orders/by-number/{order_number}", response_model=OrderResponse)
async def get_order_by_number(
    order_number: Annotated[str, Path(min_length=1, max_length=32)],
    user: CurrentUser,
    session: SessionDep,
) -> OrderResponse:
    order = await session.scalar(
        select(Order).where(
            Order.order_number == order_number,
            Order.organization_id == user.organization_id,
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return await _order_response(order, user.organization_id, session)


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> OrderResponse:
    order = await session.scalar(
        select(Order).where(
            Order.id == order_id,
            Order.organization_id == user.organization_id,
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return await _order_response(order, user.organization_id, session)


@router.get("/orders/{order_id}/shipments", response_model=list[ShipmentResponse])
async def list_shipments(
    order_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> list[ShipmentResponse]:
    order_exists = await session.scalar(
        select(Order.id).where(
            Order.id == order_id,
            Order.organization_id == user.organization_id,
        )
    )
    if order_exists is None:
        raise HTTPException(status_code=404, detail="Order not found")
    shipments = await session.scalars(
        select(Shipment)
        .where(
            Shipment.order_id == order_id,
            Shipment.organization_id == user.organization_id,
        )
        .order_by(Shipment.created_at)
    )
    return [ShipmentResponse.model_validate(shipment) for shipment in shipments]


@router.get("/orders/{order_id}/payments", response_model=list[PaymentResponse])
async def list_payments(
    order_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> list[PaymentResponse]:
    order_exists = await session.scalar(
        select(Order.id).where(
            Order.id == order_id,
            Order.organization_id == user.organization_id,
        )
    )
    if order_exists is None:
        raise HTTPException(status_code=404, detail="Order not found")
    payments = await session.scalars(
        select(Payment)
        .where(
            Payment.order_id == order_id,
            Payment.organization_id == user.organization_id,
        )
        .order_by(Payment.created_at)
    )
    responses = []
    for payment in payments:
        refunds = await session.scalars(
            select(Refund)
            .where(
                Refund.payment_id == payment.id,
                Refund.organization_id == user.organization_id,
            )
            .order_by(Refund.created_at)
        )
        responses.append(
            PaymentResponse(
                id=payment.id,
                order_id=payment.order_id,
                provider=payment.provider,
                provider_reference=payment.provider_reference,
                status=payment.status,
                amount=payment.amount,
                currency=payment.currency,
                created_at=payment.created_at,
                refunds=[RefundResponse.model_validate(refund) for refund in refunds],
            )
        )
    return responses


@router.get("/orders/{order_id}/refunds", response_model=list[RefundResponse])
async def list_order_refunds(
    order_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> list[RefundResponse]:
    order_exists = await session.scalar(
        select(Order.id).where(
            Order.id == order_id,
            Order.organization_id == user.organization_id,
        )
    )
    if order_exists is None:
        raise HTTPException(status_code=404, detail="Order not found")
    refunds = await session.scalars(
        select(Refund)
        .join(Payment, Payment.id == Refund.payment_id)
        .where(
            Payment.order_id == order_id,
            Payment.organization_id == user.organization_id,
            Refund.organization_id == user.organization_id,
        )
        .order_by(Refund.created_at)
    )
    return [RefundResponse.model_validate(refund) for refund in refunds]


@router.get("/support/tickets", response_model=list[SupportTicketResponse])
async def list_support_tickets(
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[SupportTicketResponse]:
    tickets = await session.scalars(
        select(SupportTicket)
        .where(SupportTicket.organization_id == user.organization_id)
        .order_by(SupportTicket.created_at.desc(), SupportTicket.id)
        .limit(limit)
        .offset(offset)
    )
    return [SupportTicketResponse.model_validate(ticket) for ticket in tickets]


@router.post(
    "/support/tickets",
    response_model=SupportTicketResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_support_ticket(
    payload: SupportTicketCreate,
    user: SupportAgent,
    session: SessionDep,
) -> SupportTicketResponse:
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == payload.customer_id,
            Customer.organization_id == user.organization_id,
        )
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if payload.order_id is not None:
        order_exists = await session.scalar(
            select(Order.id).where(
                Order.id == payload.order_id,
                Order.customer_id == customer.id,
                Order.organization_id == user.organization_id,
            )
        )
        if order_exists is None:
            raise HTTPException(status_code=404, detail="Order not found for this customer")

    ticket_id = uuid4()
    ticket = SupportTicket(
        id=ticket_id,
        organization_id=user.organization_id,
        customer_id=customer.id,
        order_id=payload.order_id,
        ticket_number=f"TKT-{ticket_id.hex[:8].upper()}",
        subject=payload.subject,
        description=payload.description,
        priority=payload.priority.value,
        status=SupportTicketStatus.OPEN.value,
    )
    session.add(ticket)
    await session.commit()
    await session.refresh(ticket)
    return SupportTicketResponse.model_validate(ticket)


@router.get("/support/tickets/{ticket_id}", response_model=SupportTicketResponse)
async def get_support_ticket(
    ticket_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> SupportTicketResponse:
    ticket = await session.scalar(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.organization_id == user.organization_id,
        )
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Support ticket not found")
    return SupportTicketResponse.model_validate(ticket)
