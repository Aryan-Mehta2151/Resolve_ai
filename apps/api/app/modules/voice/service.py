import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.phone import normalize_e164_phone_number
from app.db.models import (
    AgentWorkflow,
    Conversation,
    ConversationMessage,
    Customer,
    MembershipRole,
    Organization,
    OrganizationMembership,
    User,
    VoiceCall,
    VoiceCustomerVerification,
    VoicePhoneNumber,
    VoiceTurn,
    WorkflowEvent,
)
from app.modules.agent.dispatch import dispatch_workflow
from app.modules.customer.email import send_voice_verification_code
from app.modules.voice.schemas import VapiMessage

logger = logging.getLogger(__name__)

_CODE_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_CODE_DIGIT_TOKEN = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|[0-9])"
_SPOKEN_CODE_PATTERN = re.compile(
    rf"(?<![a-z0-9])({_CODE_DIGIT_TOKEN}(?:[\s,.-]+{_CODE_DIGIT_TOKEN}){{5}})(?![a-z0-9])",
    re.IGNORECASE,
)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _extract_verification_code(utterance: str) -> str | None:
    numeric_match = re.search(r"(?<!\d)(\d{6})(?!\d)", utterance)
    if numeric_match is not None:
        return numeric_match.group(1)

    spoken_match = _SPOKEN_CODE_PATTERN.search(utterance)
    if spoken_match is None:
        return None
    tokens = re.findall(_CODE_DIGIT_TOKEN, spoken_match.group(1), re.IGNORECASE)
    return "".join(_CODE_DIGIT_WORDS.get(token.lower(), token) for token in tokens)


def _verification_code_hash(call_id: UUID, code: str) -> str:
    secret = settings.jwt_secret_key
    if secret is None:
        raise HTTPException(503, "Phone caller verification is not configured")
    material = f"resolveai:voice-verification:{call_id}:{code}".encode()
    return hmac.new(secret.get_secret_value().encode(), material, hashlib.sha256).hexdigest()


async def _bind_phone_call(session: AsyncSession, message: VapiMessage) -> VoiceCall:
    provider_number_id = message.call.phoneNumberId
    caller = message.call.customer.get("number")
    if not provider_number_id:
        raise HTTPException(403, "Inbound phone number is not identified")
    phone_number = await session.scalar(
        select(VoicePhoneNumber)
        .where(VoicePhoneNumber.provider_number_id == provider_number_id)
        .with_for_update()
    )
    if phone_number is None:
        raise HTTPException(403, "Inbound phone number is not configured")

    existing = await session.scalar(
        select(VoiceCall).where(VoiceCall.provider_call_id == message.call.id).with_for_update()
    )
    if existing is not None:
        if (
            existing.channel != "phone"
            or existing.organization_id != phone_number.organization_id
            or existing.phone_number_config_id != phone_number.id
        ):
            raise HTTPException(409, "Provider call is already bound to another session")
        return existing
    if not phone_number.is_active:
        raise HTTPException(403, "Inbound phone number is disabled")

    active_actor = await session.scalar(
        select(OrganizationMembership.id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == phone_number.organization_id,
            OrganizationMembership.user_id == phone_number.service_user_id,
            OrganizationMembership.status == "active",
            OrganizationMembership.role.in_(
                ["admin", "supervisor", "support_agent"]
            ),
            User.is_active.is_(True),
        )
    )
    if active_actor is None:
        raise HTTPException(403, "Inbound phone support actor is not authorized")

    caller_number = None
    if isinstance(caller, str):
        try:
            caller_number = normalize_e164_phone_number(caller)
        except ValueError:
            caller_number = None

    candidate_ids = []
    if caller_number is not None:
        candidate_ids = list(
            await session.scalars(
                select(Customer.id)
                .join(
                    OrganizationMembership,
                    (OrganizationMembership.organization_id == Customer.organization_id)
                    & (OrganizationMembership.customer_id == Customer.id),
                )
                .join(User, User.id == OrganizationMembership.user_id)
                .where(
                    Customer.organization_id == phone_number.organization_id,
                    Customer.phone_number_normalized == caller_number,
                    OrganizationMembership.role == MembershipRole.CUSTOMER.value,
                    OrganizationMembership.status == "active",
                    User.is_active.is_(True),
                )
                .limit(2)
            )
        )
    candidate_customer_id = candidate_ids[0] if len(candidate_ids) == 1 else None
    conversation = Conversation(
        organization_id=phone_number.organization_id,
        created_by_user_id=phone_number.service_user_id,
        customer_id=None,
        status="open",
    )
    session.add(conversation)
    await session.flush()
    call = VoiceCall(
        organization_id=phone_number.organization_id,
        conversation_id=conversation.id,
        requester_id=phone_number.service_user_id,
        token_hash=None,
        provider_call_id=message.call.id,
        channel="phone",
        phone_number_config_id=phone_number.id,
        candidate_customer_id=candidate_customer_id,
        status="connecting",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(call)
    await session.flush()
    return call


async def bind_call(session: AsyncSession, message: VapiMessage) -> VoiceCall:
    if message.call.type == "inboundPhoneCall":
        call = await _bind_phone_call(session, message)
    else:
        values = message.call.assistantOverrides.get("variableValues", {})
        token = values.get("resolve_session") if isinstance(values, dict) else None
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise HTTPException(403, "Missing voice session binding")
        call = await session.scalar(
            select(VoiceCall).where(VoiceCall.token_hash == token_hash(token)).with_for_update()
        )
        if call is None:
            raise HTTPException(403, "Invalid voice session binding")
        expires = call.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            raise HTTPException(410, "Voice session expired")
        if call.provider_call_id not in {None, message.call.id}:
            raise HTTPException(409, "Voice session is already bound to another call")

    if call is None:
        raise HTTPException(403, "Voice session is not available")
    membership_result = await session.execute(
        select(
            OrganizationMembership.role,
            OrganizationMembership.customer_id,
            Conversation.customer_id,
        )
        .join(User, User.id == OrganizationMembership.user_id)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .join(
            Conversation,
            (Conversation.organization_id == OrganizationMembership.organization_id)
            & (Conversation.id == call.conversation_id),
        )
        .where(
            OrganizationMembership.organization_id == call.organization_id,
            OrganizationMembership.user_id == call.requester_id,
            OrganizationMembership.status == "active",
            OrganizationMembership.role.in_(
                ["admin", "supervisor", "support_agent", "customer"]
            ),
            User.is_active.is_(True),
            Organization.is_active.is_(True),
        )
    )
    active_membership = membership_result.one_or_none()
    if active_membership is None:
        raise HTTPException(403, "Voice session requester is no longer authorized")
    role, membership_customer_id, conversation_customer_id = active_membership
    if role == MembershipRole.CUSTOMER.value and (
        membership_customer_id is None or membership_customer_id != conversation_customer_id
    ):
        raise HTTPException(403, "Customer voice session is not linked to the requester")
    call.provider_call_id = message.call.id
    return call


async def verify_phone_customer(
    session: AsyncSession,
    call: VoiceCall,
    message: VapiMessage,
    tool_call_id: str,
) -> dict | None:
    if call.channel != "phone":
        return None
    if call.last_verification_tool_id == tool_call_id and call.last_verification_result:
        return call.last_verification_result

    async def remember(response: dict) -> dict:
        call.last_verification_tool_id = tool_call_id
        call.last_verification_result = response
        await session.commit()
        return response

    if call.status == "ended":
        return await remember({
            "status": "ended",
            "message": "This call has ended. Please start a new call for further help.",
        })
    if call.customer_verified_at is not None:
        supplied_code = _extract_verification_code(latest_utterance(message))
        if supplied_code is None or call.candidate_customer_id is None:
            return None
        verification = await session.scalar(
            select(VoiceCustomerVerification)
            .where(
                VoiceCustomerVerification.organization_id == call.organization_id,
                VoiceCustomerVerification.call_id == call.id,
                VoiceCustomerVerification.customer_id == call.candidate_customer_id,
                VoiceCustomerVerification.used_at.is_not(None),
                VoiceCustomerVerification.expires_at > call.customer_verified_at,
            )
            .order_by(VoiceCustomerVerification.created_at.desc())
            .limit(1)
        )
        supplied_hash = _verification_code_hash(call.id, supplied_code)
        if verification is None or not hmac.compare_digest(
            supplied_hash, verification.code_hash
        ):
            return None
        verified_result = call.last_verification_result
        if not isinstance(verified_result, dict) or verified_result.get("status") != "verified":
            verified_result = {
                "status": "verified",
                "message": "Your identity is verified. What can I help you with?",
            }
        return await remember(verified_result)

    if call.phone_consent_at is None:
        utterance = latest_utterance(message)
        consent = re.sub(r"[^a-z ]", "", utterance.lower()).strip()
        if consent in {"yes", "i agree", "i consent", "yes i agree", "yes i consent", "continue"}:
            call.phone_consent_at = datetime.now(UTC)
            return await remember({
                "status": "consent_granted",
                "message": "Thank you. What can I help you with?",
            })
        if consent in {"no", "i do not agree", "i dont agree", "no thank you"}:
            call.status = "ended"
            call.ended_at = datetime.now(UTC)
            call.ended_reason = "caller-declined-consent"
            return await remember({
                "status": "consent_declined",
                "message": "I will not continue without your consent. You may hang up now.",
            })
        return await remember({
            "status": "consent_required",
            "message": (
                "This call uses AI voice processing and is not recorded by ResolveAI. "
                "Do you agree to continue?"
            ),
        })

    if call.candidate_customer_id is None:
        return await remember({
            "status": "verification_unavailable",
            "message": (
                "I could not securely match this caller to an account. "
                "Please use the customer portal or contact support."
            ),
        })

    now = datetime.now(UTC)
    verification = await session.scalar(
        select(VoiceCustomerVerification)
        .where(
            VoiceCustomerVerification.organization_id == call.organization_id,
            VoiceCustomerVerification.call_id == call.id,
            VoiceCustomerVerification.customer_id == call.candidate_customer_id,
            VoiceCustomerVerification.used_at.is_(None),
        )
        .order_by(VoiceCustomerVerification.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if verification is not None:
        expires_at = verification.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            verification.used_at = now
            await session.commit()
            verification = None

    if verification is None:
        cooldown_start = now - timedelta(
            seconds=settings.voice_verification_resend_cooldown_seconds
        )
        recent = await session.scalar(
            select(VoiceCustomerVerification.id).where(
                VoiceCustomerVerification.organization_id == call.organization_id,
                VoiceCustomerVerification.customer_id == call.candidate_customer_id,
                VoiceCustomerVerification.created_at >= cooldown_start,
            )
        )
        if recent is not None:
            return await remember({
                "status": "verification_rate_limited",
                "message": (
                    "A verification email was sent recently. Please wait a minute "
                    "before trying again."
                ),
            })

        customer_result = await session.execute(
            select(Customer, Organization.name)
            .join(
                OrganizationMembership,
                (OrganizationMembership.organization_id == Customer.organization_id)
                & (OrganizationMembership.customer_id == Customer.id),
            )
            .join(User, User.id == OrganizationMembership.user_id)
            .join(Organization, Organization.id == Customer.organization_id)
            .where(
                Customer.organization_id == call.organization_id,
                Customer.id == call.candidate_customer_id,
                OrganizationMembership.role == MembershipRole.CUSTOMER.value,
                OrganizationMembership.status == "active",
                User.is_active.is_(True),
                Organization.is_active.is_(True),
            )
        )
        customer_record = customer_result.one_or_none()
        if customer_record is None:
            return await remember({
                "status": "verification_unavailable",
                "message": "I could not verify this account. Please contact support.",
            })
        customer, organization_name = customer_record
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            await send_voice_verification_code(customer.email, organization_name, code)
        except (OSError, RuntimeError) as exc:
            logger.error(
                "Unable to deliver voice verification code",
                extra={
                    "organization_id": str(call.organization_id),
                    "error_type": type(exc).__name__,
                },
            )
            raise HTTPException(
                503, "Unable to send a verification email right now"
            ) from exc
        response = {
            "status": "verification_required",
            "message": (
                "For your security, I sent a six-digit verification code to the email "
                "address on the account. Please say the code."
            ),
        }
        session.add(
            VoiceCustomerVerification(
                organization_id=call.organization_id,
                call_id=call.id,
                customer_id=customer.id,
                code_hash=_verification_code_hash(call.id, code),
                expires_at=now + timedelta(minutes=settings.voice_verification_minutes),
            )
        )
        call.last_verification_tool_id = tool_call_id
        call.last_verification_result = response
        await session.commit()
        return response

    utterance = latest_utterance(message)
    supplied_code = _extract_verification_code(utterance)
    if supplied_code is None:
        return await remember({
            "status": "verification_required",
            "message": "Please say the six-digit verification code from your email.",
        })
    supplied_hash = _verification_code_hash(call.id, supplied_code)
    if not hmac.compare_digest(supplied_hash, verification.code_hash):
        verification.attempt_count += 1
        if verification.attempt_count >= 5:
            verification.used_at = now
            response = {
                "status": "verification_failed",
                "message": "I could not verify the code. Please contact support.",
            }
        else:
            response = {
                "status": "verification_failed",
                "message": "That code did not match. Please try again.",
            }
        return await remember(response)

    conversation = await session.scalar(
        select(Conversation)
        .where(
            Conversation.id == call.conversation_id,
            Conversation.organization_id == call.organization_id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise HTTPException(404, "Voice conversation was not found")
    verification.used_at = now
    call.customer_verified_at = now
    conversation.customer_id = call.candidate_customer_id
    response = {
        "status": "verified",
        "message": "Your identity is verified. What can I help you with?",
    }
    call.last_verification_tool_id = tool_call_id
    call.last_verification_result = response
    await session.commit()
    return response


def latest_utterance(message: VapiMessage) -> str:
    messages = message.artifact.get("messages", [])
    if not isinstance(messages, list):
        raise HTTPException(422, "Provider transcript messages are required")
    for item in reversed(messages):
        if isinstance(item, dict) and item.get("role") == "user":
            content = item.get("message")
            if isinstance(content, str) and 0 < len(content.strip()) <= 4000:
                return content.strip()
            break
    raise HTTPException(
        422, "A bounded actual user transcript is required; tool text is not trusted"
    )


async def latest_workflow(session: AsyncSession, call: VoiceCall) -> UUID | None:
    return await session.scalar(
        select(AgentWorkflow.id)
        .join(ConversationMessage, ConversationMessage.id == AgentWorkflow.trigger_message_id)
        .where(
            AgentWorkflow.organization_id == call.organization_id,
            AgentWorkflow.conversation_id == call.conversation_id,
        )
        .order_by(ConversationMessage.sequence_number.desc())
        .limit(1)
    )


async def request_support(
    session: AsyncSession, call: VoiceCall, message: VapiMessage, tool_id: str
) -> UUID:
    prior = await session.scalar(
        select(VoiceTurn).where(
            VoiceTurn.call_id == call.id,
            VoiceTurn.organization_id == call.organization_id,
            VoiceTurn.provider_tool_id == tool_id,
        )
    )
    if prior is not None:
        return prior.workflow_id
    if call.status == "ended":
        raise HTTPException(409, "Call has ended")
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == call.conversation_id,
            Conversation.organization_id == call.organization_id,
        ).with_for_update()
    )
    if conversation is None or conversation.status != "open":
        raise HTTPException(409, "Conversation requires human review or is closed")
    content = latest_utterance(message)
    active = await session.scalar(
        select(AgentWorkflow.id).where(
            AgentWorkflow.conversation_id == call.conversation_id,
            AgentWorkflow.organization_id == call.organization_id,
            AgentWorkflow.status.in_(["queued", "running"]),
        )
    )
    if active is not None:
        trigger_content = await session.scalar(
            select(ConversationMessage.content)
            .join(AgentWorkflow, AgentWorkflow.trigger_message_id == ConversationMessage.id)
            .where(AgentWorkflow.id == active)
        )
        if trigger_content != content:
            raise HTTPException(409, "Wait for the current support request before a new request")
        session.add(VoiceTurn(
            organization_id=call.organization_id,
            call_id=call.id,
            workflow_id=active,
            provider_tool_id=tool_id,
        ))
        await session.commit()
        return active
    sequence = await session.scalar(
        select(func.max(ConversationMessage.sequence_number)).where(
            ConversationMessage.conversation_id == call.conversation_id,
            ConversationMessage.organization_id == call.organization_id,
        )
    )
    trigger = ConversationMessage(
        id=uuid4(),
        organization_id=call.organization_id,
        conversation_id=call.conversation_id,
        sequence_number=(sequence or 0) + 1,
        role="user",
        content=content,
    )
    session.add(trigger)
    await session.flush()
    workflow = AgentWorkflow(
        id=uuid4(),
        organization_id=call.organization_id,
        conversation_id=call.conversation_id,
        requested_by_user_id=call.requester_id,
        trigger_message_id=trigger.id,
        status="queued",
    )
    session.add(workflow)
    await session.flush()
    session.add_all([
        VoiceTurn(
            organization_id=call.organization_id,
            call_id=call.id,
            workflow_id=workflow.id,
            provider_tool_id=tool_id,
        ),
        WorkflowEvent(
            organization_id=call.organization_id,
            workflow_id=workflow.id,
            sequence_number=1,
            event_type="workflow.queued",
            payload={"conversation_id": str(call.conversation_id), "channel": "voice"},
        ),
    ])
    await session.commit()
    await dispatch_workflow(session, workflow)
    return workflow.id


async def support_result(
    session: AsyncSession, call: VoiceCall, workflow_id: UUID | None
) -> dict:
    if workflow_id is None:
        return {"status": "not_started", "message": "No support request has been submitted."}
    deadline = monotonic() + settings.vapi_result_wait_seconds
    while True:
        workflow = await session.scalar(
            select(AgentWorkflow).where(
                AgentWorkflow.id == workflow_id,
                AgentWorkflow.organization_id == call.organization_id,
                AgentWorkflow.conversation_id == call.conversation_id,
            ).execution_options(populate_existing=True)
        )
        if workflow is None:
            raise HTTPException(404, "Voice workflow not found")
        if workflow.status == "completed":
            trigger = await session.get(ConversationMessage, workflow.trigger_message_id)
            answer = await session.scalar(
                select(ConversationMessage.content).where(
                    ConversationMessage.conversation_id == call.conversation_id,
                    ConversationMessage.organization_id == call.organization_id,
                    ConversationMessage.role == "assistant",
                    ConversationMessage.sequence_number == trigger.sequence_number + 1,
                )
            )
            if answer is None:
                raise HTTPException(409, "Completed workflow response is unavailable")
            return {"status": "completed", "answer": answer}
        if workflow.status == "failed":
            return {
                "status": "failed",
                "error_code": workflow.error_code,
                "message": "The support workflow failed. Do not claim an action succeeded.",
            }
        if monotonic() >= deadline:
            return {
                "status": "pending",
                "message": "Still processing. Use check_support_result; do not resubmit.",
            }
        await session.commit()
        await asyncio.sleep(min(0.25, max(0, deadline - monotonic())))
