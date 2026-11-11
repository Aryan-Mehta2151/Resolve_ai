import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from test_tools import auth_headers, register

from app.core.celery import celery_app
from app.core.config import settings
from app.db.models import (
    AgentWorkflow,
    Conversation,
    ConversationMessage,
    VoiceCall,
    VoiceCustomerVerification,
)
from app.modules.voice import service as voice_service


def test_extract_verification_code_accepts_numeric_and_spoken_digits() -> None:
    assert voice_service._extract_verification_code("My code is 012345.") == "012345"
    assert (
        voice_service._extract_verification_code(
            "My code is zero, one, two, three, four, five."
        )
        == "012345"
    )
    assert voice_service._extract_verification_code("My code is 0 1 2 3 4 5.") == "012345"
    assert voice_service._extract_verification_code("My code is one hundred and five.") is None


def _verified_customer(client, monkeypatch, slug: str, suffix: str) -> tuple[dict, dict, dict]:
    from app.modules.auth import router as auth_router

    tenant = register(
        client,
        email=f"admin-{suffix}@example.com",
        slug=slug,
    )
    headers = auth_headers(tenant["access_token"])
    customer_response = client.post(
        "/api/v1/customers",
        headers=headers,
        json={
            "full_name": "Phone Customer",
            "email": f"phone-customer-{suffix}@example.com",
            "phone_number": "+1 (555) 123-4567",
        },
    )
    assert customer_response.status_code == 201, customer_response.text
    customer = customer_response.json()

    delivered: list[tuple[str, str, str]] = []

    async def send_account_verification(recipient: str, organization: str, token: str) -> None:
        delivered.append((recipient, organization, token))

    monkeypatch.setattr(settings, "customer_email_smtp_host", "mailpit")
    monkeypatch.setattr(settings, "customer_email_smtp_starttls", False)
    monkeypatch.setattr(
        settings,
        "customer_email_verification_url",
        "http://localhost:3000/customer",
    )
    monkeypatch.setattr(auth_router, "send_verification_email", send_account_verification)
    payload = {
        "organization_slug": slug,
        "email": customer["email"],
        "password": "Customer-Strong-Password-1!",
    }
    assert client.post("/api/v1/customer/auth/register", json=payload).status_code == 202
    assert delivered
    verified = client.post(
        "/api/v1/customer/auth/verify",
        json={"token": delivered[0][2]},
    )
    assert verified.status_code == 200, verified.text
    login = client.post("/api/v1/customer/auth/login", json=payload)
    assert login.status_code == 200, login.text
    return tenant, customer, {
        "staff": headers,
        "customer": auth_headers(login.json()["access_token"]),
    }


def _phone_payload(
    *,
    event: str,
    call_id: str = "inbound-call-1",
    tool_id: str = "phone-tool-1",
    text: str = "Where is my order?",
    caller_number: str = "+15551234567",
    status_value: str | None = None,
) -> dict:
    message = {
        "type": event,
        "call": {
            "id": call_id,
            "assistantId": "test-assistant",
            "type": "inboundPhoneCall",
            "phoneNumberId": "vapi-number-1",
            "customer": {"number": caller_number},
        },
    }
    if status_value is not None:
        message["status"] = status_value
    if event == "tool-calls":
        message["toolCallList"] = [
            {
                "id": tool_id,
                "function": {"name": "resolve_support", "arguments": {}},
            }
        ]
        message["artifact"] = {"messages": [{"role": "user", "message": text}]}
    return {"message": message}


def _post_phone_event(client, payload: dict):
    webhook_secret = settings.vapi_webhook_secret
    assert webhook_secret is not None
    return client.post(
        "/api/v1/voice/webhook",
        headers={"Authorization": f"Bearer {webhook_secret.get_secret_value()}"},
        json=payload,
    )


def _result(response) -> dict:
    assert response.status_code == 200, response.text
    return json.loads(response.json()["results"][0]["result"])


def _configure_vapi(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vapi_enabled", True)
    monkeypatch.setattr(settings, "vapi_public_key", "test-public-key")
    monkeypatch.setattr(settings, "vapi_assistant_id", "test-assistant")
    monkeypatch.setattr(
        settings,
        "vapi_webhook_secret",
        SecretStr("test-webhook-" + "x" * 32),
    )
    monkeypatch.setattr(settings, "vapi_public_api_url", "https://api.example.com")
    monkeypatch.setattr(settings, "vapi_result_wait_seconds", 0)
    monkeypatch.setattr(settings, "agent_intent_provider", "deterministic")


def _register_vapi_number(client, tenant: dict, staff_headers: dict) -> dict:
    response = client.post(
        "/api/v1/voice/phone-numbers",
        headers=staff_headers,
        json={
            "provider_number_id": "vapi-number-1",
            "phone_number": "+1 (555) 999-0100",
            "service_user_id": tenant["user"]["id"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_inbound_phone_verifies_caller_before_support_and_links_call(
    client,
    database_session_factory,
    monkeypatch,
) -> None:
    tenant, customer, headers = _verified_customer(
        client, monkeypatch, "phone-tenant", "phone"
    )
    _configure_vapi(monkeypatch)
    _register_vapi_number(client, tenant, headers["staff"])

    sent_codes: list[tuple[str, str, str]] = []

    async def send_code(recipient: str, organization: str, code: str) -> None:
        sent_codes.append((recipient, organization, code))

    monkeypatch.setattr(voice_service, "send_voice_verification_code", send_code)
    dispatched: list[str] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda *args, **kwargs: dispatched.append(kwargs["args"][0]),
    )

    started = _post_phone_event(
        client,
        _phone_payload(event="status-update", status_value="ringing"),
    )
    assert started.status_code == 200, started.text
    call_history = client.get("/api/v1/voice/calls", headers=headers["staff"])
    assert call_history.status_code == 200
    call = call_history.json()[0]
    assert call["channel"] == "phone"

    consent_required = _result(
        _post_phone_event(
            client,
            _phone_payload(event="tool-calls", tool_id="consent-initial"),
        )
    )
    assert consent_required["status"] == "consent_required"
    consented = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                tool_id="consent-accepted",
                text="I agree.",
            ),
        )
    )
    assert consented["status"] == "consent_granted"
    requested = _result(
        _post_phone_event(
            client,
            _phone_payload(event="tool-calls", tool_id="verification-start"),
        )
    )
    assert requested["status"] == "verification_required"
    assert len(sent_codes) == 1
    assert sent_codes[0][0] == customer["email"]
    assert sent_codes[0][2].isdigit() and len(sent_codes[0][2]) == 6
    assert dispatched == []

    incorrect = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                tool_id="verification-wrong",
                text=f"My code is {'000000' if sent_codes[0][2] != '000000' else '000001'}.",
            ),
        )
    )
    assert incorrect["status"] == "verification_failed"
    digit_words = {
        "0": "zero",
        "1": "one",
        "2": "two",
        "3": "three",
        "4": "four",
        "5": "five",
        "6": "six",
        "7": "seven",
        "8": "eight",
        "9": "nine",
    }
    spoken_code = " ".join(digit_words[digit] for digit in sent_codes[0][2])
    verified = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                tool_id="verification-success",
                text=f"My verification code is {spoken_code}.",
            ),
        )
    )
    assert verified["status"] == "verified"
    assert _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                tool_id="verification-success",
                text=f"My verification code is {sent_codes[0][2]}.",
            ),
        )
    ) == verified
    assert _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                tool_id="verification-success-retry",
                text=f"My verification code is {sent_codes[0][2]}.",
            ),
        )
    ) == verified
    assert dispatched == []

    customer_calls = client.get(
        "/api/v1/customer/voice/calls",
        headers=headers["customer"],
    )
    assert customer_calls.status_code == 200
    assert customer_calls.json()[0]["id"] == call["id"]
    assert customer_calls.json()[0]["channel"] == "phone"

    support = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                tool_id="support-after-verification",
                text="Where is my order?",
            ),
        )
    )
    assert support["status"] == "pending"
    assert len(dispatched) == 1

    async def verify_persistence() -> None:
        async with database_session_factory() as session:
            stored_call = await session.scalar(
                select(VoiceCall).where(VoiceCall.provider_call_id == "inbound-call-1")
            )
            assert stored_call is not None
            assert stored_call.customer_verified_at is not None
            conversation = await session.get(Conversation, stored_call.conversation_id)
            assert conversation is not None
            assert str(conversation.customer_id) == customer["id"]
            messages = list(await session.scalars(select(ConversationMessage)))
            assert [message.content for message in messages] == ["Where is my order?"]
            verification = await session.scalar(select(VoiceCustomerVerification))
            assert verification is not None
            assert verification.code_hash != sent_codes[0][2]
            assert verification.used_at is not None
            workflow = await session.scalar(select(AgentWorkflow))
            assert workflow is not None
            assert workflow.requested_by_user_id == UUID(tenant["user"]["id"])

    asyncio.run(verify_persistence())


def test_inbound_phone_unknown_caller_fails_closed_and_number_mapping_is_tenant_scoped(
    client,
    monkeypatch,
) -> None:
    tenant, _, headers = _verified_customer(
        client, monkeypatch, "phone-tenant-isolated", "isolated"
    )
    _configure_vapi(monkeypatch)
    number = _register_vapi_number(client, tenant, headers["staff"])
    sent_codes: list[str] = []

    async def send_code(recipient: str, organization: str, code: str) -> None:
        sent_codes.append(code)

    monkeypatch.setattr(voice_service, "send_voice_verification_code", send_code)

    other_tenant = register(
        client,
        email="other-phone-admin@example.com",
        slug="other-phone-tenant",
    )
    other_headers = auth_headers(other_tenant["access_token"])
    assert client.get(
        "/api/v1/voice/phone-numbers",
        headers=other_headers,
    ).json() == []
    assert client.delete(
        f"/api/v1/voice/phone-numbers/{number['id']}",
        headers=other_headers,
    ).status_code == 404
    assert client.get(
        "/api/v1/voice/phone-numbers",
        headers=headers["customer"],
    ).status_code == 403

    assert _post_phone_event(
        client,
        _phone_payload(
            event="status-update",
            call_id="unknown-caller-call",
            caller_number="+15550001111",
            status_value="ringing",
        ),
    ).status_code == 200
    consent = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                call_id="unknown-caller-call",
                tool_id="unknown-caller-consent",
                caller_number="+15550001111",
                text="I agree.",
            ),
        )
    )
    assert consent["status"] == "consent_granted"
    denied = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                call_id="unknown-caller-call",
                tool_id="unknown-caller-tool",
                caller_number="+15550001111",
            ),
        )
    )
    assert denied["status"] == "verification_unavailable"
    assert sent_codes == []
    phone_call = client.get(
        "/api/v1/voice/calls",
        headers=headers["staff"],
    ).json()[0]
    assert client.post(
        f"/api/v1/voice/calls/{phone_call['id']}/end",
        headers=headers["staff"],
    ).status_code == 409
    assert client.delete(
        f"/api/v1/voice/phone-numbers/{number['id']}",
        headers=headers["staff"],
    ).status_code == 204
    assert _post_phone_event(
        client,
        _phone_payload(
            event="end-of-call-report",
            call_id="unknown-caller-call",
            caller_number="+15550001111",
        ),
    ).status_code == 200
    assert _post_phone_event(
        client,
        _phone_payload(
            event="status-update",
            call_id="disabled-number-call",
            caller_number="+15550001111",
            status_value="ringing",
        ),
    ).status_code == 403


def test_phone_verification_expiry_and_attempt_limit(
    client,
    database_session_factory,
    monkeypatch,
) -> None:
    tenant, _, headers = _verified_customer(
        client, monkeypatch, "phone-expiry-tenant", "expiry"
    )
    _configure_vapi(monkeypatch)
    _register_vapi_number(client, tenant, headers["staff"])
    sent_codes: list[str] = []

    async def send_code(recipient: str, organization: str, code: str) -> None:
        sent_codes.append(code)

    monkeypatch.setattr(voice_service, "send_voice_verification_code", send_code)
    dispatched: list[str] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda *args, **kwargs: dispatched.append(kwargs["args"][0]),
    )
    call_id = "verification-expiry-call"
    assert _post_phone_event(
        client,
        _phone_payload(event="status-update", call_id=call_id, status_value="ringing"),
    ).status_code == 200
    assert _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                call_id=call_id,
                tool_id="expiry-consent",
                text="I agree.",
            ),
        )
    )["status"] == "consent_granted"
    assert _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                call_id=call_id,
                tool_id="expiry-verification-start",
            ),
        )
    )["status"] == "verification_required"
    assert len(sent_codes) == 1

    async def expire_challenge() -> None:
        async with database_session_factory() as session, session.begin():
            verification = await session.scalar(
                select(VoiceCustomerVerification).where(
                    VoiceCustomerVerification.call_id
                    == (await session.scalar(
                        select(VoiceCall.id).where(VoiceCall.provider_call_id == call_id)
                    ))
                )
            )
            assert verification is not None
            verification.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            verification.created_at = datetime.now(UTC) - timedelta(minutes=2)

    asyncio.run(expire_challenge())
    refreshed_code = _result(
        _post_phone_event(
            client,
            _phone_payload(
                event="tool-calls",
                call_id=call_id,
                tool_id="expiry-resend",
                text="I need a new code.",
            ),
        )
    )
    assert refreshed_code["status"] == "verification_required"
    assert len(sent_codes) == 2

    wrong_code = "000000" if sent_codes[1] != "000000" else "000001"
    responses = []
    for attempt in range(5):
        responses.append(
            _result(
                _post_phone_event(
                    client,
                    _phone_payload(
                        event="tool-calls",
                        call_id=call_id,
                        tool_id=f"locked-attempt-{attempt}",
                        text=f"My code is {wrong_code}.",
                    ),
                )
            )
        )
    assert responses[-1]["message"] == "I could not verify the code. Please contact support."
    assert dispatched == []

    async def verify_lockout() -> None:
        async with database_session_factory() as session:
            verification = await session.scalar(
                select(VoiceCustomerVerification)
                .where(VoiceCustomerVerification.call_id == (
                    await session.scalar(
                        select(VoiceCall.id).where(VoiceCall.provider_call_id == call_id)
                    )
                ))
                .order_by(VoiceCustomerVerification.created_at.desc())
                .limit(1)
            )
            assert verification is not None
            assert verification.attempt_count == 5
            assert verification.used_at is not None

    asyncio.run(verify_lockout())
