from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.core.phone import normalize_e164_phone_number
from app.db.models import (
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    ShipmentStatus,
    SupportTicketPriority,
    SupportTicketStatus,
)


class CustomerResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    phone_number: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CustomerCreateRequest(BaseModel):
    full_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    email: EmailStr
    phone_number: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
    ] = None

    @field_validator("phone_number")
    @classmethod
    def normalize_phone_number(cls, value: str | None) -> str | None:
        try:
            return normalize_e164_phone_number(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ProductResponse(BaseModel):
    id: UUID
    sku: str
    name: str
    description: str
    unit_price: Decimal
    stock_quantity: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class OrderItemResponse(BaseModel):
    id: UUID
    product_id: UUID
    quantity: int
    unit_price: Decimal

    model_config = ConfigDict(from_attributes=True)


class OrderResponse(BaseModel):
    id: UUID
    customer_id: UUID
    order_number: str
    status: OrderStatus
    total_amount: Decimal
    currency: str
    created_at: datetime
    items: list[OrderItemResponse] = Field(default_factory=list)


class ShipmentResponse(BaseModel):
    id: UUID
    order_id: UUID
    tracking_number: str
    carrier: str
    status: ShipmentStatus
    estimated_delivery_at: datetime | None
    shipped_at: datetime | None
    delivered_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class RefundResponse(BaseModel):
    id: UUID
    payment_id: UUID
    idempotency_key: str
    status: RefundStatus
    amount: Decimal
    reason: str
    provider_reference: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    provider: str
    provider_reference: str
    status: PaymentStatus
    amount: Decimal
    currency: str
    created_at: datetime
    refunds: list[RefundResponse] = Field(default_factory=list)


class SupportTicketCreate(BaseModel):
    customer_id: UUID
    order_id: UUID | None = None
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=180)]
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=5000),
    ]
    priority: SupportTicketPriority = SupportTicketPriority.NORMAL


class SupportTicketResponse(BaseModel):
    id: UUID
    customer_id: UUID
    order_id: UUID | None
    ticket_number: str
    subject: str
    description: str
    status: SupportTicketStatus
    priority: SupportTicketPriority
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
