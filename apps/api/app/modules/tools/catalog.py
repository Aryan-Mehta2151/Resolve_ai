import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import perf_counter
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.models import (
    AgentWorkflow,
    ConversationMessage,
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
    ToolExecution,
    WorkflowEvent,
)
from app.modules.commerce.workflows import BusinessRuleError, apply_action
from app.modules.tools.order_confirmation import is_order_confirmation
from app.modules.tools.schemas import (
    BusinessActionInput,
    BusinessActionOutput,
    ConfirmOrderInput,
    CreateSupportTicketToolInput,
    CreateSupportTicketToolOutput,
    DemoOrderOutput,
    DemoProduct,
    OrderQuoteOutput,
    OrderStatusToolInput,
    OrderStatusToolOutput,
    ProductListInput,
    ProductListOutput,
    QuoteOrderInput,
    RefundStatusToolInput,
    RefundStatusToolOutput,
    RefundSummary,
    ShipmentStatusSummary,
)

logger = logging.getLogger(__name__)


async def _request_business_action(
    session: AsyncSession,
    context: "ToolExecutionContext",
    payload: dict[str, object],
) -> BusinessActionOutput:
    if not settings.demo_business_workflows_enabled:
        raise ToolFailure("demo_workflows_disabled", "Demo business workflows are disabled.")
    request = BusinessActionInput.model_validate(payload)
    if context.customer_id is None:
        raise ToolFailure("customer_required", "Start a conversation with a customer attached.")
    message = await session.scalar(
        select(ConversationMessage.content)
        .join(
            AgentWorkflow,
            AgentWorkflow.trigger_message_id == ConversationMessage.id,
        )
        .where(
            AgentWorkflow.id == context.workflow_id,
            AgentWorkflow.organization_id == context.organization_id,
            AgentWorkflow.conversation_id == context.conversation_id,
        )
    )
    prefix = "confirm cancel" if request.kind == "cancellation" else "request refund"
    if not message or not re.fullmatch(
        rf"{prefix}\s+{re.escape(request.order_number)}",
        message.strip(),
        re.I,
    ):
        raise ToolFailure("confirmation_required", f'Say "{prefix} {request.order_number}".')
    order = await session.scalar(
        select(Order).where(
            Order.organization_id == context.organization_id,
            Order.customer_id == context.customer_id,
            Order.order_number == request.order_number.upper(),
        )
    )
    if order is None:
        raise ToolFailure("order_not_found", "No matching order exists for this customer.")
    try:
        action = await apply_action(
            session,
            context.organization_id,
            order.id,
            context.actor_user_id,
            request.kind,
            "Customer requested through chat",
        )
    except BusinessRuleError as exc:
        raise ToolFailure("business_rule_denied", str(exc)) from exc
    return BusinessActionOutput(
        order_number=order.order_number,
        kind=action.kind,
        status=action.status,
    )


async def _list_products(
    session: AsyncSession,
    context: "ToolExecutionContext",
    payload: dict[str, object],
) -> ProductListOutput:
    products = await session.scalars(
        select(Product)
        .where(
            Product.organization_id == context.organization_id,
            Product.is_active.is_(True),
        )
        .order_by(Product.sku)
        .limit(50)
    )
    return ProductListOutput(
        products=[
            DemoProduct(
                sku=p.sku, name=p.name, unit_price=p.unit_price, stock_quantity=p.stock_quantity
            )
            for p in products
        ]
    )


async def _quote_order(
    session: AsyncSession,
    context: "ToolExecutionContext",
    payload: dict[str, object],
) -> OrderQuoteOutput:
    request = QuoteOrderInput.model_validate(payload)
    customer = await session.scalar(
        select(Customer).where(
            Customer.organization_id == context.organization_id, Customer.id == context.customer_id
        )
    )
    if customer is None:
        raise ToolFailure(
            "conversation_customer_required",
        "Select a customer for this conversation before placing an order. "
        "A customer attached to the conversation is required.",
    )
    normalized_sku = request.product.lower().replace("-", "").replace(" ", "")
    products = list(
        await session.scalars(
            select(Product)
            .where(
                Product.organization_id == context.organization_id,
                Product.is_active.is_(True),
                (func.lower(Product.sku) == request.product.lower())
                | (func.lower(Product.name) == request.product.lower())
                | (
                    func.replace(func.replace(func.lower(Product.sku), "-", ""), " ", "")
                    == normalized_sku
                ),
            )
            .limit(2)
        )
    )
    if not products:
        description = " ".join(request.product.lower().split())
        products = list(
            await session.scalars(
                select(Product)
                .where(
                    Product.organization_id == context.organization_id,
                    Product.is_active.is_(True),
                    (" " + func.lower(Product.name) + " ").contains(
                        f" {description} ", autoescape=True
                    ),
                )
                .limit(2)
            )
        )
    if not products:
        raise ToolFailure(
            "product_not_found", "Product not found. Ask to list products and use its SKU."
        )
    if len(products) != 1:
        raise ToolFailure("ambiguous_product", "More than one product matches. Use its exact SKU.")
    product = products[0]
    if product.stock_quantity < request.quantity:
        raise ToolFailure("insufficient_stock", "That quantity is not in stock.")
    return OrderQuoteOutput(
        quote_id=context.workflow_id,
        product_id=product.id,
        customer_id=customer.id,
        customer_name=customer.full_name,
        sku=product.sku,
        name=product.name,
        quantity=request.quantity,
        unit_price=product.unit_price,
        total_amount=product.unit_price * request.quantity,
    )


async def _confirm_order(
    session: AsyncSession,
    context: "ToolExecutionContext",
    payload: dict[str, object],
) -> DemoOrderOutput:
    ConfirmOrderInput.model_validate(payload)
    message = await session.scalar(
        select(ConversationMessage.content)
        .join(AgentWorkflow, AgentWorkflow.trigger_message_id == ConversationMessage.id)
        .where(
            AgentWorkflow.id == context.workflow_id,
            AgentWorkflow.organization_id == context.organization_id,
            AgentWorkflow.conversation_id == context.conversation_id,
        )
    )
    if not message or not is_order_confirmation(message):
        raise ToolFailure("confirmation_required", 'Review the summary, then say "confirm order".')
    execution = await session.scalar(
        select(ToolExecution)
        .join(AgentWorkflow, AgentWorkflow.id == ToolExecution.workflow_id)
        .join(ConversationMessage, ConversationMessage.id == AgentWorkflow.trigger_message_id)
        .where(
            ToolExecution.organization_id == context.organization_id,
            ToolExecution.tool_name == "commerce_quote_demo_order",
            AgentWorkflow.conversation_id == context.conversation_id,
            AgentWorkflow.status == "completed",
            ConversationMessage.sequence_number
            < (
                select(ConversationMessage.sequence_number)
                .join(AgentWorkflow, AgentWorkflow.trigger_message_id == ConversationMessage.id)
                .where(AgentWorkflow.id == context.workflow_id)
                .correlate(None)
                .scalar_subquery()
            ),
        )
        .order_by(ConversationMessage.sequence_number.desc(), ToolExecution.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if execution is None or execution.status != "succeeded" or execution.result_payload is None:
        raise ToolFailure(
            "quote_not_found", "No matching order summary exists in this conversation."
        )
    quote = OrderQuoteOutput.model_validate(execution.result_payload)
    if quote.customer_id != context.customer_id:
        raise ToolFailure("customer_mismatch", "The order summary belongs to a different customer.")
    customer = await session.scalar(
        select(Customer).where(
            Customer.organization_id == context.organization_id,
            Customer.id == quote.customer_id,
        )
    )
    if customer is None:
        raise ToolFailure(
            "customer_not_found", "The customer for this order is no longer available."
        )
    number = f"ORD-D-{quote.quote_id.hex[:26].upper()}"
    existing = await session.scalar(
        select(Order).where(
            Order.organization_id == context.organization_id, Order.order_number == number
        )
    )
    if existing:
        return DemoOrderOutput(
            order_number=existing.order_number,
            customer_name=customer.full_name,
            total_amount=existing.total_amount,
            currency=existing.currency,
            status=existing.status,
        )
    if execution.created_at.replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(minutes=15):
        raise ToolFailure("quote_expired", "This summary expired. Request a new order summary.")
    product = await session.scalar(
        select(Product)
        .where(
            Product.organization_id == context.organization_id,
            Product.id == quote.product_id,
            Product.is_active.is_(True),
        )
        .with_for_update()
    )
    if product is None or product.stock_quantity < quote.quantity:
        raise ToolFailure(
            "insufficient_stock", "The product is no longer available in that quantity."
        )
    if product.unit_price != quote.unit_price:
        raise ToolFailure(
            "price_changed", "The price changed. Request a new summary before confirming."
        )
    order = Order(
        organization_id=context.organization_id,
        customer_id=quote.customer_id,
        order_number=number,
        status="pending",
        total_amount=quote.total_amount,
        currency=quote.currency,
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            organization_id=context.organization_id,
            order_id=order.id,
            product_id=product.id,
            quantity=quote.quantity,
            unit_price=quote.unit_price,
        )
    )
    product.stock_quantity -= quote.quantity
    return DemoOrderOutput(
        order_number=number,
        customer_name=customer.full_name,
        total_amount=quote.total_amount,
        currency=quote.currency,
        status="pending",
    )


class ToolCategory(StrEnum):
    CUSTOMER = "customer"
    COMMERCE = "commerce"
    KNOWLEDGE = "knowledge"
    SUPPORT = "support"
    NOTIFICATIONS = "notifications"


class ToolOperation(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True)
class ToolExecutionContext:
    organization_id: UUID
    actor_user_id: UUID
    role: MembershipRole
    workflow_id: UUID
    conversation_id: UUID
    customer_id: UUID | None
    session_factory: async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    category: ToolCategory
    operation: ToolOperation
    allowed_roles: frozenset[MembershipRole]
    timeout_seconds: float
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    idempotent: bool
    handler: Callable[[AsyncSession, ToolExecutionContext, dict[str, object]], Awaitable[BaseModel]]


async def _get_order_status(
    session: AsyncSession,
    context: ToolExecutionContext,
    payload: dict[str, object],
) -> OrderStatusToolOutput:
    request = OrderStatusToolInput.model_validate(payload)
    statement = select(Order).where(
        Order.organization_id == context.organization_id,
        Order.order_number == request.order_number,
    )
    if context.role == MembershipRole.CUSTOMER:
        if context.customer_id is None:
            raise ToolFailure("customer_required", "Customer profile is unavailable.")
        statement = statement.where(Order.customer_id == context.customer_id)
    order = await session.scalar(statement)
    if order is None:
        raise ToolFailure("order_not_found", "No matching order was found.")
    shipments = await session.scalars(
        select(Shipment)
        .where(
            Shipment.organization_id == context.organization_id,
            Shipment.order_id == order.id,
        )
        .order_by(Shipment.created_at, Shipment.id)
    )
    return OrderStatusToolOutput(
        order_number=order.order_number,
        status=order.status,
        shipments=[
            ShipmentStatusSummary(
                carrier=shipment.carrier,
                status=shipment.status,
                estimated_delivery_at=shipment.estimated_delivery_at,
            )
            for shipment in shipments
        ],
    )


async def _get_refund_status(
    session: AsyncSession,
    context: ToolExecutionContext,
    payload: dict[str, object],
) -> RefundStatusToolOutput:
    request = RefundStatusToolInput.model_validate(payload)
    order = await session.scalar(
        select(Order).where(
            Order.organization_id == context.organization_id,
            Order.order_number == request.order_number,
        )
    )
    if order is None:
        raise ToolFailure("order_not_found", "No matching order was found.")
    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.organization_id == context.organization_id,
            Payment.order_id == order.id,
        )
        .order_by(Payment.created_at.desc(), Payment.id)
        .limit(1)
    )
    if payment is None:
        return RefundStatusToolOutput(
            order_number=order.order_number,
            payment_status=None,
        )
    refunds = await session.scalars(
        select(Refund)
        .where(
            Refund.organization_id == context.organization_id,
            Refund.payment_id == payment.id,
        )
        .order_by(Refund.created_at, Refund.id)
    )
    return RefundStatusToolOutput(
        order_number=order.order_number,
        payment_status=payment.status,
        refunds=[RefundSummary(status=refund.status) for refund in refunds],
    )


async def _create_support_ticket(
    session: AsyncSession,
    context: ToolExecutionContext,
    payload: dict[str, object],
) -> CreateSupportTicketToolOutput:
    request = CreateSupportTicketToolInput.model_validate(payload)
    if context.customer_id is None:
        raise ToolFailure(
            "conversation_customer_required",
            "A customer must be linked before creating a support ticket.",
        )

    order_id = None
    if request.order_number is not None:
        order_id = await session.scalar(
            select(Order.id).where(
                Order.organization_id == context.organization_id,
                Order.customer_id == context.customer_id,
                Order.order_number == request.order_number,
            )
        )
        if order_id is None:
            raise ToolFailure(
                "customer_order_not_found",
                "No matching order was found for the linked customer.",
            )

    ticket_id = uuid4()
    ticket = SupportTicket(
        id=ticket_id,
        organization_id=context.organization_id,
        customer_id=context.customer_id,
        order_id=order_id,
        ticket_number=f"TKT-{ticket_id.hex[:8].upper()}",
        subject=request.subject,
        description=request.description,
        status=SupportTicketStatus.OPEN.value,
        priority=request.priority.value,
    )
    session.add(ticket)
    await session.flush()
    return CreateSupportTicketToolOutput(
        ticket_number=ticket.ticket_number,
        status=SupportTicketStatus(ticket.status),
    )


class ToolCatalog:
    def __init__(self) -> None:
        support_roles = frozenset(
            {
                MembershipRole.ADMIN,
                MembershipRole.SUPERVISOR,
                MembershipRole.SUPPORT_AGENT,
            }
        )
        order_roles = support_roles | {MembershipRole.CUSTOMER}
        all_roles = frozenset(MembershipRole)
        self._definitions = {
            "commerce_request_action": ToolDefinition(
                name="commerce_request_action",
                description="Request a confirmed mock cancellation or approval-gated refund.",
                category=ToolCategory.COMMERCE,
                operation=ToolOperation.WRITE,
                allowed_roles=support_roles,
                timeout_seconds=5.0,
                input_model=BusinessActionInput,
                output_model=BusinessActionOutput,
                idempotent=True,
                handler=_request_business_action,
            ),
            "commerce_list_products": ToolDefinition(
                name="commerce_list_products",
                description="List demo products and USD prices.",
                category=ToolCategory.COMMERCE,
                operation=ToolOperation.READ,
                allowed_roles=all_roles,
                timeout_seconds=3.0,
                input_model=ProductListInput,
                output_model=ProductListOutput,
                idempotent=True,
                handler=_list_products,
            ),
            "commerce_quote_demo_order": ToolDefinition(
                name="commerce_quote_demo_order",
                description="Prepare a demo order summary without creating an order or charging.",
                category=ToolCategory.COMMERCE,
                operation=ToolOperation.WRITE,
                allowed_roles=order_roles,
                timeout_seconds=3.0,
                input_model=QuoteOrderInput,
                output_model=OrderQuoteOutput,
                idempotent=True,
                handler=_quote_order,
            ),
            "commerce_confirm_demo_order": ToolDefinition(
                name="commerce_confirm_demo_order",
                description="Create a demo order only after an explicit customer confirmation.",
                category=ToolCategory.COMMERCE,
                operation=ToolOperation.WRITE,
                allowed_roles=order_roles,
                timeout_seconds=3.0,
                input_model=ConfirmOrderInput,
                output_model=DemoOrderOutput,
                idempotent=True,
                handler=_confirm_order,
            ),
            "commerce_get_order_status": ToolDefinition(
                name="commerce_get_order_status",
                description=(
                    "Look up an order and its shipment status within the active organization."
                ),
                category=ToolCategory.COMMERCE,
                operation=ToolOperation.READ,
                allowed_roles=all_roles,
                timeout_seconds=3.0,
                input_model=OrderStatusToolInput,
                output_model=OrderStatusToolOutput,
                idempotent=True,
                handler=_get_order_status,
            ),
            "commerce_get_refund_status": ToolDefinition(
                name="commerce_get_refund_status",
                description=(
                    "Read payment and refund statuses for an order in the active organization."
                ),
                category=ToolCategory.COMMERCE,
                operation=ToolOperation.READ,
                allowed_roles=support_roles,
                timeout_seconds=3.0,
                input_model=RefundStatusToolInput,
                output_model=RefundStatusToolOutput,
                idempotent=True,
                handler=_get_refund_status,
            ),
            "support_create_ticket": ToolDefinition(
                name="support_create_ticket",
                description=(
                    "Create an idempotent support ticket for the customer "
                    "linked to this conversation."
                ),
                category=ToolCategory.SUPPORT,
                operation=ToolOperation.WRITE,
                allowed_roles=order_roles,
                timeout_seconds=3.0,
                input_model=CreateSupportTicketToolInput,
                output_model=CreateSupportTicketToolOutput,
                idempotent=True,
                handler=_create_support_ticket,
            ),
        }

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def available_to(self, role: MembershipRole) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition in self._definitions.values()
            if role in definition.allowed_roles
        )

    async def invoke(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> BaseModel:
        definition = self.get(name)
        if definition is None:
            raise ToolFailure("unknown_tool", "The requested tool is unavailable.")
        try:
            parsed_arguments = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            await self._record_failure(context, definition, "invalid_arguments", 0)
            raise ToolFailure("invalid_arguments", "Tool arguments are invalid.") from exc
        if context.role not in definition.allowed_roles:
            await self._record_failure(context, definition, "permission_denied", 0)
            raise ToolFailure("permission_denied", "The current role cannot use this tool.")

        idempotency_key = None
        if definition.operation == ToolOperation.WRITE and definition.idempotent:
            idempotency_key = f"{context.workflow_id}:{definition.name}"
        if idempotency_key is not None:
            async with context.session_factory() as session:
                prior = await session.scalar(
                    select(ToolExecution).where(
                        ToolExecution.organization_id == context.organization_id,
                        ToolExecution.idempotency_key == idempotency_key,
                    )
                )
            if prior is not None:
                if (
                    prior.status == ToolExecutionStatus.SUCCEEDED.value
                    and prior.result_payload is not None
                ):
                    await self._record_event(
                        context,
                        "tool.replayed",
                        {"tool_name": definition.name, "operation": definition.operation.value},
                    )
                    return definition.output_model.model_validate(prior.result_payload)
                raise ToolFailure("idempotency_conflict", "This tool action was already attempted.")

        started_at = perf_counter()
        await self._record_event(
            context,
            "tool.started",
            {"tool_name": definition.name, "operation": definition.operation.value},
        )
        try:
            async with asyncio.timeout(definition.timeout_seconds):
                async with context.session_factory() as session, session.begin():
                    result = await definition.handler(
                        session,
                        context,
                        parsed_arguments.model_dump(),
                    )
                    if not isinstance(result, definition.output_model):
                        raise TypeError(f"{definition.name} returned an invalid output model")
                    duration_ms = int((perf_counter() - started_at) * 1000)
                    session.add(
                        ToolExecution(
                            organization_id=context.organization_id,
                            workflow_id=context.workflow_id,
                            actor_user_id=context.actor_user_id,
                            tool_name=definition.name,
                            operation=definition.operation.value,
                            status=ToolExecutionStatus.SUCCEEDED.value,
                            idempotency_key=idempotency_key,
                            duration_ms=duration_ms,
                            result_payload=(
                                result.model_dump(mode="json")
                                if definition.operation == ToolOperation.WRITE
                                else None
                            ),
                        )
                    )
                    await session.flush()
                    await self._record_workflow_event(
                        session,
                        context,
                        "tool.completed",
                        {
                            "tool_name": definition.name,
                            "operation": definition.operation.value,
                            "duration_ms": duration_ms,
                        },
                    )
            logger.info(
                "Agent tool execution completed",
                extra={
                    "organization_id": str(context.organization_id),
                    "actor_user_id": str(context.actor_user_id),
                    "workflow_id": str(context.workflow_id),
                    "tool_name": definition.name,
                    "duration_ms": duration_ms,
                    "outcome": "succeeded",
                },
            )
            return result
        except ToolFailure as exc:
            duration_ms = int((perf_counter() - started_at) * 1000)
            await self._record_failure(context, definition, exc.code, duration_ms)
            raise
        except TimeoutError as exc:
            duration_ms = int((perf_counter() - started_at) * 1000)
            await self._record_failure(context, definition, "tool_timeout", duration_ms)
            raise ToolFailure("tool_timeout", "The tool did not finish in time.") from exc
        except Exception as exc:
            duration_ms = int((perf_counter() - started_at) * 1000)
            await self._record_failure(context, definition, "tool_execution_failed", duration_ms)
            logger.error(
                "Agent tool execution failed",
                extra={
                    "organization_id": str(context.organization_id),
                    "actor_user_id": str(context.actor_user_id),
                    "workflow_id": str(context.workflow_id),
                    "tool_name": definition.name,
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                },
            )
            raise ToolFailure("tool_execution_failed", "The tool could not be completed.") from exc

    async def _record_failure(
        self,
        context: ToolExecutionContext,
        definition: ToolDefinition,
        error_code: str,
        duration_ms: int,
    ) -> None:
        async with context.session_factory() as session, session.begin():
            session.add(
                ToolExecution(
                    organization_id=context.organization_id,
                    workflow_id=context.workflow_id,
                    actor_user_id=context.actor_user_id,
                    tool_name=definition.name,
                    operation=definition.operation.value,
                    status=ToolExecutionStatus.FAILED.value,
                    duration_ms=duration_ms,
                    error_code=error_code,
                )
            )
            await self._record_workflow_event(
                session,
                context,
                "tool.failed",
                {"tool_name": definition.name, "error_code": error_code},
            )
        logger.warning(
            "Agent tool execution was denied or failed",
            extra={
                "organization_id": str(context.organization_id),
                "actor_user_id": str(context.actor_user_id),
                "workflow_id": str(context.workflow_id),
                "tool_name": definition.name,
                "duration_ms": duration_ms,
                "error_code": error_code,
            },
        )

    async def _record_event(
        self,
        context: ToolExecutionContext,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        async with context.session_factory() as session, session.begin():
            await self._record_workflow_event(session, context, event_type, payload)

    @staticmethod
    async def _record_workflow_event(
        session: AsyncSession,
        context: ToolExecutionContext,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        latest_sequence = await session.scalar(
            select(func.max(WorkflowEvent.sequence_number)).where(
                WorkflowEvent.organization_id == context.organization_id,
                WorkflowEvent.workflow_id == context.workflow_id,
            )
        )
        session.add(
            WorkflowEvent(
                organization_id=context.organization_id,
                workflow_id=context.workflow_id,
                sequence_number=(latest_sequence or 0) + 1,
                event_type=event_type,
                payload=payload,
            )
        )


tool_catalog = ToolCatalog()
