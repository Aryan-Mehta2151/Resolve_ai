from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import PaymentStatus, RefundStatus, SupportTicketPriority, SupportTicketStatus

OrderNumber = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9-]+$",
    ),
]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderStatusToolInput(ToolInput):
    order_number: OrderNumber


class ShipmentStatusSummary(BaseModel):
    carrier: str
    status: str
    estimated_delivery_at: datetime | None


class OrderStatusToolOutput(BaseModel):
    order_number: str
    status: str
    shipments: list[ShipmentStatusSummary] = Field(default_factory=list)


class RefundStatusToolInput(ToolInput):
    order_number: OrderNumber


class RefundSummary(BaseModel):
    status: RefundStatus


class RefundStatusToolOutput(BaseModel):
    order_number: str
    payment_status: PaymentStatus | None
    refunds: list[RefundSummary] = Field(default_factory=list)


class CreateSupportTicketToolInput(ToolInput):
    subject: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=180),
    ]
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
    ]
    priority: SupportTicketPriority = SupportTicketPriority.NORMAL
    order_number: OrderNumber | None = None


class CreateSupportTicketToolOutput(BaseModel):
    ticket_number: str
    status: SupportTicketStatus


class ProductListInput(ToolInput):
    pass


class DemoProduct(BaseModel):
    sku: str
    name: str
    unit_price: Decimal
    stock_quantity: int


class ProductListOutput(BaseModel):
    products: list[DemoProduct]


class QuoteOrderInput(ToolInput):
    product: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    quantity: int = Field(ge=1, le=100)


class OrderQuoteOutput(BaseModel):
    quote_id: UUID
    product_id: UUID
    customer_id: UUID
    customer_name: str | None = None
    sku: str
    name: str
    quantity: int
    unit_price: Decimal
    total_amount: Decimal
    currency: str = "USD"


class ConfirmOrderInput(ToolInput):
    pass


class DemoOrderOutput(BaseModel):
    order_number: str
    customer_name: str
    total_amount: Decimal
    currency: str
    status: str
    demo: bool = True
    payment_charged: bool = False


class BusinessActionInput(ToolInput):
    order_number: OrderNumber
    kind: Literal["cancellation", "refund"]


class BusinessActionOutput(BaseModel):
    order_number: str
    kind: str
    status: str
    demo: bool = True
