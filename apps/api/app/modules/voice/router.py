import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.auth import AuthenticatedUser, CurrentUser, SessionDep, require_roles
from app.core.config import settings
from app.db.models import (
    Conversation,
    Customer,
    MembershipRole,
    OrganizationMembership,
    User,
    VoiceCall,
    VoicePhoneNumber,
)
from app.modules.commerce.router import SupportAgent
from app.modules.customer.email import email_configuration_errors
from app.modules.voice.provider import assistant_configuration, voice_readiness_issues
from app.modules.voice.schemas import (
    CallCreate,
    CallResponse,
    CallStarted,
    VapiWebhook,
    VoicePhoneNumberCreate,
    VoicePhoneNumberResponse,
)
from app.modules.voice.service import (
    bind_call,
    latest_workflow,
    request_support,
    support_result,
    token_hash,
    verify_phone_customer,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/voice", tags=["voice demo"])
AdminUser = Annotated[AuthenticatedUser, Depends(require_roles(MembershipRole.ADMIN))]


def _reject(status_code: int, detail: str, message: VapiWebhook | None = None) -> HTTPException:
    # Logs metadata only (no transcript text or secrets) so a rejected Vapi event can be diagnosed.
    if message is None:
        logger.warning("Voice webhook rejected: status=%s detail=%s", status_code, detail)
    else:
        payload = message.message
        logger.warning(
            "Voice webhook rejected: status=%s detail=%s type=%s call_status=%s "
            "ended_reason=%s tools=%s artifact_keys=%s",
            status_code,
            detail,
            payload.type,
            payload.status,
            payload.endedReason,
            [tool.function.name for tool in payload.toolCallList],
            sorted(payload.artifact.keys()),
        )
    return HTTPException(status_code, detail)


@router.get("/config")
async def voice_config(user: CurrentUser, session: SessionDep) -> dict:
    errors = await voice_readiness_issues()
    phone_calls_enabled = bool(
        await session.scalar(
            select(VoicePhoneNumber.id).where(
                VoicePhoneNumber.organization_id == user.organization_id,
                VoicePhoneNumber.is_active.is_(True),
            )
        )
    )
    return {
        "ready": not errors,
        "issues": errors,
        "mode": "browser_and_phone" if phone_calls_enabled else "browser",
        "recording_policy": "disabled_in_assistant_template",
        "phone_calls_enabled": phone_calls_enabled,
    }


@router.get("/phone-numbers", response_model=list[VoicePhoneNumberResponse])
async def list_phone_numbers(
    user: AdminUser, session: SessionDep
) -> list[VoicePhoneNumberResponse]:
    numbers = await session.scalars(
        select(VoicePhoneNumber)
        .where(VoicePhoneNumber.organization_id == user.organization_id)
        .order_by(VoicePhoneNumber.created_at.desc(), VoicePhoneNumber.id)
    )
    return [VoicePhoneNumberResponse.model_validate(number) for number in numbers]


@router.post(
    "/phone-numbers",
    response_model=VoicePhoneNumberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_phone_number(
    payload: VoicePhoneNumberCreate,
    user: AdminUser,
    session: SessionDep,
) -> VoicePhoneNumberResponse:
    email_errors = email_configuration_errors()
    if email_errors:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Phone caller verification is not configured",
                "issues": email_errors,
            },
        )
    actor = await session.scalar(
        select(OrganizationMembership.id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == user.organization_id,
            OrganizationMembership.user_id == payload.service_user_id,
            OrganizationMembership.status == "active",
            OrganizationMembership.role.in_(
                [
                    MembershipRole.ADMIN.value,
                    MembershipRole.SUPERVISOR.value,
                    MembershipRole.SUPPORT_AGENT.value,
                ]
            ),
            User.is_active.is_(True),
        )
    )
    if actor is None:
        raise HTTPException(422, "Service user must be an active support staff member")
    number = VoicePhoneNumber(
        organization_id=user.organization_id,
        provider_number_id=payload.provider_number_id,
        phone_number=payload.phone_number,
        service_user_id=payload.service_user_id,
    )
    session.add(number)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Provider phone number is already configured") from exc
    await session.refresh(number)
    return VoicePhoneNumberResponse.model_validate(number)


@router.delete("/phone-numbers/{number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_phone_number(
    number_id: UUID,
    user: AdminUser,
    session: SessionDep,
) -> None:
    number = await session.scalar(
        select(VoicePhoneNumber)
        .where(
            VoicePhoneNumber.id == number_id,
            VoicePhoneNumber.organization_id == user.organization_id,
        )
        .with_for_update()
    )
    if number is None:
        raise HTTPException(404, "Phone number not found")
    number.is_active = False
    await session.commit()


@router.get("/assistant-template")
async def assistant_template(user: SupportAgent, credential_id: str = Query(min_length=1)) -> dict:
    return assistant_configuration(credential_id)


@router.post("/calls", response_model=CallStarted, status_code=201)
async def create_call(
    body: CallCreate, user: SupportAgent, session: SessionDep
) -> CallStarted:
    errors = await voice_readiness_issues()
    if errors:
        raise HTTPException(503, {"message": "Voice calling is not configured", "issues": errors})
    if body.customer_id is not None:
        customer = await session.scalar(
            select(Customer.id).where(
                Customer.id == body.customer_id, Customer.organization_id == user.organization_id
            )
        )
        if customer is None:
            raise HTTPException(404, "Customer not found")
    conversation = Conversation(
        id=uuid4(),
        organization_id=user.organization_id,
        created_by_user_id=user.user_id,
        customer_id=body.customer_id,
        status="open",
    )
    session.add(conversation)
    await session.flush()
    token = secrets.token_urlsafe(32)
    call = VoiceCall(
        organization_id=user.organization_id,
        conversation_id=conversation.id,
        requester_id=user.user_id,
        token_hash=token_hash(token),
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
        session_token=token,
    )


@router.get("/calls", response_model=list[CallResponse])
async def list_calls(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CallResponse]:
    calls = await session.scalars(
        select(VoiceCall).where(VoiceCall.organization_id == user.organization_id)
        .order_by(VoiceCall.created_at.desc(), VoiceCall.id).limit(limit).offset(offset)
    )
    return [CallResponse.model_validate(call) for call in calls]


@router.post("/calls/{call_id}/end", response_model=CallResponse)
async def end_local_call(
    call_id: UUID, user: SupportAgent, session: SessionDep
) -> CallResponse:
    call = await session.scalar(
        select(VoiceCall).where(
            VoiceCall.id == call_id, VoiceCall.organization_id == user.organization_id
        ).with_for_update()
    )
    if call is None:
        raise HTTPException(404, "Call not found")
    if call.channel != "browser":
        raise HTTPException(409, "Phone calls must be ended by the voice provider")
    if call.status != "ended":
        call.status = "ended"
        call.ended_at = datetime.now(UTC)
        call.ended_reason = "browser-session-closed"
    await session.commit()
    return CallResponse.model_validate(call)


@router.post("/webhook")
async def vapi_webhook(
    body: VapiWebhook,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    if not settings.vapi_enabled or settings.vapi_webhook_secret is None:
        raise HTTPException(503, "Voice webhooks are not enabled")
    expected = settings.vapi_webhook_secret.get_secret_value()
    if len(expected) < 32:
        raise HTTPException(503, "Voice webhook authentication is not configured")
    if authorization is None or not secrets.compare_digest(
        authorization.encode(), f"Bearer {expected}".encode()
    ):
        logger.warning("Rejected unauthenticated voice webhook")
        raise HTTPException(401, "Invalid voice webhook authentication")
    message = body.message
    if message.call.assistantId != settings.vapi_assistant_id:
        raise _reject(403, "Unexpected voice assistant", body)
    if message.type not in {"status-update", "end-of-call-report", "tool-calls"}:
        raise _reject(422, "Unsupported voice event", body)
    try:
        call = await bind_call(session, message)
    except HTTPException as exc:
        raise _reject(exc.status_code, str(exc.detail), body) from exc
    if message.type != "tool-calls":
        if message.type == "end-of-call-report" or message.status == "ended":
            if call.status != "ended":
                call.status = "ended"
                call.ended_at = datetime.now(UTC)
            if message.endedReason:
                call.ended_reason = message.endedReason
        elif call.status != "ended":
            ranks = {"created": 0, "connecting": 1, "in-progress": 2}
            normalized = {
                "scheduled": "connecting", "queued": "connecting",
                "ringing": "connecting", "in-progress": "in-progress",
            }.get(message.status or "")
            if normalized is None:
                raise _reject(422, "Unsupported voice call status", body)
            if ranks[normalized] > ranks[call.status]:
                call.status = normalized
        await session.commit()
        return {"ok": True}
    if len(message.toolCallList) != 1:
        raise _reject(422, "Send exactly one support tool call at a time", body)
    tool = message.toolCallList[0]
    if tool.function.name not in {"resolve_support", "check_support_result"}:
        raise _reject(422, "Unsupported voice tool", body)
    try:
        if tool.function.name == "resolve_support":
            verification_response = await verify_phone_customer(
                session, call, message, tool.id
            )
            if verification_response is not None:
                await session.commit()
                return {"results": [{
                    "name": tool.function.name,
                    "toolCallId": tool.id,
                    "result": json.dumps(verification_response),
                }]}
            workflow_id = await request_support(session, call, message, tool.id)
        else:
            if call.channel == "phone" and call.customer_verified_at is None:
                return {"results": [{
                    "name": tool.function.name,
                    "toolCallId": tool.id,
                    "result": json.dumps({
                        "status": "verification_required",
                        "message": (
                            "Caller verification is required before checking a support request."
                        ),
                    }),
                }]}
            workflow_id = await latest_workflow(session, call)
    except HTTPException as exc:
        raise _reject(exc.status_code, str(exc.detail), body) from exc
    await session.commit()
    result = await support_result(session, call, workflow_id)
    return {"results": [{
        "name": tool.function.name,
        "toolCallId": tool.id,
        "result": json.dumps(result),
    }]}
