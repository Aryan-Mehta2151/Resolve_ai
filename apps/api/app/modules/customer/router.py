import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, SessionDep, require_roles
from app.core.config import settings
from app.db.models import (
    Conversation,
    Customer,
    MembershipRole,
    Order,
    VoiceCall,
    VoicePhoneNumber,
)
from app.modules.commerce.router import _order_response
from app.modules.commerce.schemas import CustomerResponse, OrderResponse
from app.modules.voice.provider import voice_readiness_issues
from app.modules.voice.schemas import CallResponse, CallStarted, CustomerCallCreate
from app.modules.voice.service import token_hash

router = APIRouter(prefix="/api/v1/customer", tags=["customer portal"])
CustomerUser = Annotated[
    AuthenticatedUser,
    Depends(require_roles(MembershipRole.CUSTOMER)),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


async def _customer_profile(user: AuthenticatedUser, session: AsyncSession) -> Customer:
    if user.customer_id is None:
        raise HTTPException(status_code=403, detail="Customer profile is not linked")
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == user.customer_id,
            Customer.organization_id == user.organization_id,
        )
    )
    if customer is None:
        raise HTTPException(status_code=403, detail="Customer profile is not available")
    return customer


@router.get("/profile", response_model=CustomerResponse)
async def get_profile(user: CustomerUser, session: SessionDep) -> CustomerResponse:
    customer = await _customer_profile(user, session)
    return CustomerResponse.model_validate(customer)


@router.get("/orders", response_model=list[OrderResponse])
async def list_my_orders(
    user: CustomerUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[OrderResponse]:
    customer = await _customer_profile(user, session)
    orders = await session.scalars(
        select(Order)
        .where(
            Order.organization_id == user.organization_id,
            Order.customer_id == customer.id,
        )
        .order_by(Order.created_at.desc(), Order.id)
        .limit(limit)
        .offset(offset)
    )
    return [await _order_response(order, user.organization_id, session) for order in orders]


@router.get("/voice/config")
async def customer_voice_config(user: CustomerUser, session: SessionDep) -> dict[str, object]:
    issues = await voice_readiness_issues()
    phone_calls_enabled = bool(
        await session.scalar(
            select(VoicePhoneNumber.id).where(
                VoicePhoneNumber.organization_id == user.organization_id,
                VoicePhoneNumber.is_active.is_(True),
            )
        )
    )
    return {
        "ready": not issues,
        "issues": issues,
        "mode": "browser_and_phone" if phone_calls_enabled else "browser",
        "recording_policy": "disabled_in_assistant_template",
        "phone_calls_enabled": phone_calls_enabled,
    }


@router.post("/voice/calls", response_model=CallStarted, status_code=201)
async def create_customer_call(
    body: CustomerCallCreate,
    user: CustomerUser,
    session: SessionDep,
) -> CallStarted:
    customer = await _customer_profile(user, session)
    issues = await voice_readiness_issues()
    if issues:
        raise HTTPException(
            status_code=503,
            detail={"message": "Voice calling is not configured", "issues": issues},
        )
    conversation = Conversation(
        id=uuid4(),
        organization_id=user.organization_id,
        created_by_user_id=user.user_id,
        customer_id=customer.id,
        status="open",
    )
    session.add(conversation)
    await session.flush()
    raw_token = secrets.token_urlsafe(32)
    call = VoiceCall(
        organization_id=user.organization_id,
        conversation_id=conversation.id,
        requester_id=user.user_id,
        token_hash=token_hash(raw_token),
        status="created",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return CallStarted(
        call=CallResponse.model_validate(call),
        public_key=settings.vapi_public_key,
        assistant_id=settings.vapi_assistant_id,
        session_token=raw_token,
    )


@router.get("/voice/calls", response_model=list[CallResponse])
async def list_customer_calls(
    user: CustomerUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[CallResponse]:
    calls = await session.scalars(
        select(VoiceCall)
        .join(
            Conversation,
            (Conversation.organization_id == VoiceCall.organization_id)
            & (Conversation.id == VoiceCall.conversation_id),
        )
        .where(
            VoiceCall.organization_id == user.organization_id,
            Conversation.customer_id == user.customer_id,
        )
        .order_by(VoiceCall.created_at.desc(), VoiceCall.id)
        .limit(limit)
        .offset(offset)
    )
    return [CallResponse.model_validate(call) for call in calls]


@router.post("/voice/calls/{call_id}/end", response_model=CallResponse)
async def end_customer_call(
    call_id: UUID,
    user: CustomerUser,
    session: SessionDep,
) -> CallResponse:
    call = await session.scalar(
        select(VoiceCall)
        .where(
            VoiceCall.id == call_id,
            VoiceCall.organization_id == user.organization_id,
            VoiceCall.requester_id == user.user_id,
        )
        .with_for_update()
    )
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if call.status != "ended":
        call.status = "ended"
        call.ended_at = datetime.now(UTC)
        call.ended_reason = "browser-session-closed"
    await session.commit()
    return CallResponse.model_validate(call)
