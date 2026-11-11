from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BusinessAction,
    Order,
    OrderItem,
    Payment,
    Product,
    Refund,
    Shipment,
)


class BusinessRuleError(RuntimeError):
    pass


async def apply_action(
    session: AsyncSession,
    organization_id: UUID,
    order_id: UUID,
    actor_id: UUID,
    kind: str,
    reason: str,
) -> BusinessAction:
    order = await session.scalar(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.id == order_id,
        )
        .with_for_update()
    )
    if order is None:
        raise BusinessRuleError("Order not found")
    # Lock the order first: cancellations, requests and decisions serialize per order.
    prior = await session.scalar(
        select(BusinessAction).where(
            BusinessAction.organization_id == organization_id,
            BusinessAction.order_id == order_id,
            BusinessAction.kind == kind,
        )
    )
    if prior is not None:
        return prior
    payments = list(
        await session.scalars(
            select(Payment)
            .where(
                Payment.organization_id == organization_id,
                Payment.order_id == order_id,
            )
            .order_by(Payment.created_at.desc(), Payment.id)
            .with_for_update()
        )
    )
    if any(payment.provider != "mock" for payment in payments):
        raise BusinessRuleError("Only mock payments are supported; no real action was performed")
    action = BusinessAction(
        organization_id=organization_id,
        order_id=order_id,
        requester_id=actor_id,
        kind=kind,
        reason=reason,
        idempotency_key=f"{kind}:{order_id}",
    )
    if kind == "cancellation":
        if order.status not in {"pending", "processing"}:
            raise BusinessRuleError("Only pending or processing orders can be cancelled")
        shipment_exists = await session.scalar(
            select(Shipment.id)
            .where(
                Shipment.organization_id == organization_id,
                Shipment.order_id == order_id,
            )
            .limit(1)
        )
        if shipment_exists:
            raise BusinessRuleError("Orders with recorded shipments require human review")
        if payments:
            raise BusinessRuleError("Orders with payment records require human review")
        if not order.order_number.startswith("ORD-D-"):
            raise BusinessRuleError("Only demo chat orders can use automatic cancellation")
        items = list(
            await session.scalars(
                select(OrderItem)
                .where(
                    OrderItem.organization_id == organization_id,
                    OrderItem.order_id == order_id,
                )
                .order_by(OrderItem.product_id)
            )
        )
        for item in items:
            product = await session.scalar(
                select(Product)
                .where(
                    Product.organization_id == organization_id,
                    Product.id == item.product_id,
                )
                .with_for_update()
            )
            if product is None:
                raise BusinessRuleError("An order product is unavailable; contact a supervisor")
            product.stock_quantity += item.quantity
        order.status = "cancelled"
        action.status = "succeeded"
        action.decided_at = datetime.now(UTC)
    elif kind == "refund":
        payment = next(
            (p for p in payments if p.status in {"succeeded", "partially_refunded"}), None
        )
        if payment is None:
            raise BusinessRuleError("No eligible successful mock payment exists for this order")
        reserved = await session.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0)).where(
                Refund.organization_id == organization_id,
                Refund.payment_id == payment.id,
                Refund.status.not_in(["rejected", "failed"]),
            )
        )
        amount = payment.amount - Decimal(str(reserved))
        if amount <= 0:
            raise BusinessRuleError("This payment is already refunded or has a pending refund")
        refund = Refund(
            organization_id=organization_id,
            payment_id=payment.id,
            idempotency_key=f"business-refund:{order_id}",
            status="requires_approval",
            amount=amount,
            reason=reason,
        )
        session.add(refund)
        await session.flush()
        action.refund_id = refund.id
        action.status = "pending"
    else:
        raise BusinessRuleError("Unsupported business action")
    session.add(action)
    await session.flush()
    return action


async def decide_refund(
    session: AsyncSession,
    organization_id: UUID,
    action_id: UUID,
    reviewer_id: UUID,
    approve: bool,
    note: str,
) -> BusinessAction:
    action = await session.scalar(
        select(BusinessAction).where(
            BusinessAction.organization_id == organization_id,
            BusinessAction.id == action_id,
        )
    )
    if action is None:
        raise BusinessRuleError("Request not found")
    await session.scalar(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.id == action.order_id,
        )
        .with_for_update()
    )
    await session.refresh(action)
    if action.kind != "refund":
        raise BusinessRuleError("Only refund requests need approval")
    target = "succeeded" if approve else "rejected"
    if action.status != "pending":
        if action.status == target:
            return action
        raise BusinessRuleError("This request already has a different final decision")
    refund = await session.scalar(
        select(Refund)
        .where(
            Refund.organization_id == organization_id,
            Refund.id == action.refund_id,
        )
        .with_for_update()
    )
    if refund is None or refund.status != "requires_approval":
        raise BusinessRuleError("Refund is no longer awaiting approval")
    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.organization_id == organization_id,
            Payment.id == refund.payment_id,
        )
        .with_for_update()
    )
    if payment is None or payment.provider != "mock":
        raise BusinessRuleError("Only mock refunds are supported")
    if approve:
        if payment.status not in {"succeeded", "partially_refunded"}:
            raise BusinessRuleError("Payment is no longer eligible")
        refunded = await session.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0)).where(
                Refund.organization_id == organization_id,
                Refund.payment_id == payment.id,
                Refund.status == "succeeded",
            )
        )
        total = Decimal(str(refunded)) + refund.amount
        if total > payment.amount:
            raise BusinessRuleError("Refund would exceed the payment")
        refund.status = "succeeded"
        refund.provider_reference = f"MOCK-REFUND-{refund.id.hex}"
        payment.status = "refunded" if total == payment.amount else "partially_refunded"
    else:
        refund.status = "rejected"
    action.status = target
    action.reviewer_id = reviewer_id
    action.decision_note = note
    action.decided_at = datetime.now(UTC)
    await session.flush()
    return action
