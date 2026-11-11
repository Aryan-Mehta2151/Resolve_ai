import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import func, select, update
from test_tools import auth_headers, insert_order, register

from app.core.celery import celery_app
from app.core.config import settings
from app.db.models import (
    ConversationMessage,
    Order,
    OrganizationMembership,
    Product,
    VoiceCall,
    VoiceTurn,
)
from app.modules.agent.service import run_agent_workflow


@pytest.fixture
def voice_runtime(client, database_session_factory, monkeypatch):
    monkeypatch.setattr(settings, "vapi_enabled", True)
    monkeypatch.setattr(settings, "vapi_public_key", "test-public-key")
    monkeypatch.setattr(settings, "vapi_assistant_id", "test-assistant")
    monkeypatch.setattr(settings, "vapi_webhook_secret", SecretStr("test-webhook-" + "x" * 32))
    monkeypatch.setattr(settings, "vapi_public_api_url", "https://api.example.com")
    monkeypatch.setattr(settings, "vapi_result_wait_seconds", 0)
    monkeypatch.setattr(settings, "agent_intent_provider", "deterministic")
    dispatched = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: dispatched.append(kwargs["args"][0])
    )
    tenant = register(client, email="voice@example.com", slug="voice")
    org = UUID(tenant["organization"]["id"])
    customer = insert_order(database_session_factory, org, "VOICE")
    headers = auth_headers(tenant["access_token"])
    created = client.post(
        "/api/v1/voice/calls",
        headers=headers,
        json={"customer_id": str(customer), "consent": True},
    )
    assert created.status_code == 201, created.text
    return tenant, headers, created.json(), dispatched


def webhook(created, *, event="tool-calls", tool_id="tool-1", text="Where is order ORD-VOICE?"):
    return {
        "message": {
            "type": event,
            "call": {
                "id": "provider-call-1",
                "assistantId": "test-assistant",
                "type": "webCall",
                "assistantOverrides": {
                    "variableValues": {"resolve_session": created["session_token"]},
                },
            },
            "toolCallList": [{
                "id": tool_id,
                "function": {"name": "resolve_support", "arguments": {}},
            }],
            "artifact": {"messages": [{"role": "user", "message": text}]},
        },
    }


def post(client, payload):
    return client.post(
        "/api/v1/voice/webhook",
        headers={"Authorization": f"Bearer {settings.vapi_webhook_secret.get_secret_value()}"},
        json=payload,
    )


def result(response):
    assert response.status_code == 200, response.text
    return json.loads(response.json()["results"][0]["result"])


def test_voice_workflow_and_duplicate_delivery(
    client, database_session_factory, voice_runtime
):
    _, headers, created, dispatched = voice_runtime
    payload = webhook(created)
    assert result(post(client, payload))["status"] == "pending"
    assert len(dispatched) == 1
    assert result(post(client, payload))["status"] == "pending"
    assert len(dispatched) == 1
    asyncio.run(run_agent_workflow(UUID(dispatched[0]), database_session_factory, InMemorySaver()))
    response = result(post(client, payload))
    assert response["status"] == "completed"
    assert "ORD-VOICE" in response["answer"]
    assert len(dispatched) == 1
    conversation = client.get(
        f"/api/v1/conversations/{created['call']['conversation_id']}", headers=headers
    ).json()
    assert len(conversation["messages"]) == 2
    assert conversation["messages"][0]["content"] == "Where is order ORD-VOICE?"
    history = client.get("/api/v1/voice/calls", headers=headers).json()
    assert history[0]["provider_call_id"] == "provider-call-1"
    assert "token_hash" not in history[0]
    assert "session_token" not in history[0]


@pytest.mark.parametrize("message", [
    "Can you list the S K U codes?",
    "Hi, I wanted to know what products do you have.",
])
def test_voice_spoken_sku_list_reaches_catalog(
    client, database_session_factory, voice_runtime, message
):
    _, _, created, dispatched = voice_runtime
    payload = webhook(created, text=message)
    assert result(post(client, payload))["status"] == "pending"
    asyncio.run(run_agent_workflow(UUID(dispatched[0]), database_session_factory, InMemorySaver()))
    response = result(post(client, payload))
    assert response["status"] == "completed"
    assert "SKU-VOICE" in response["answer"]
    assert "25.00" in response["answer"]


def test_voice_order_requires_confirmation_and_persists_once(
    client, database_session_factory, voice_runtime
):
    tenant, headers, created, dispatched = voice_runtime
    organization_id = UUID(tenant["organization"]["id"])

    def turn(tool_id, text):
        payload = webhook(created, tool_id=tool_id, text=text)
        assert result(post(client, payload))["status"] == "pending"
        asyncio.run(
            run_agent_workflow(
                UUID(dispatched[-1]), database_session_factory, InMemorySaver()
            )
        )
        response = result(post(client, payload))
        assert response["status"] == "completed"
        return payload, response["answer"]

    _, catalog = turn("catalog", "list the SKUs")
    assert "SKU-VOICE" in catalog
    _, quote = turn("quote", "Buy 2 SKU-VOICE")
    assert "Customer VOICE" in quote
    assert "50.00 USD" in quote
    assert "No order has been created" in quote
    assert '"confirm order"' in quote
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 1
    turn("not-confirmation", "Yes.")
    assert len(client.get("/api/v1/orders", headers=headers).json()) == 1
    confirmation, answer = turn("confirmation", "Confirm order.")
    assert "created" in answer
    assert "Customer VOICE" in answer
    assert "No real payment was charged" in answer
    assert result(post(client, confirmation))["answer"] == answer
    _, repeated = turn("confirmation-again", "confirm order")
    assert repeated == answer
    orders = client.get("/api/v1/orders", headers=headers).json()
    assert len(orders) == 2

    _, second_quote = turn("quote-second", "Buy 1 SKU-VOICE")
    assert "25.00 USD" in second_quote
    assert "No order has been created" in second_quote
    _, second_order = turn("confirmation-second", "Confirm order")
    assert "created" in second_order
    assert "No real payment was charged" in second_order
    assert second_order != answer
    orders = client.get("/api/v1/orders", headers=headers).json()
    assert len(orders) == 3

    async def verify():
        async with database_session_factory() as session:
            product = await session.scalar(
                select(Product).where(
                    Product.organization_id == organization_id,
                    Product.sku == "SKU-VOICE",
                )
            )
            assert product.stock_quantity == 2
            orders = list(
                await session.scalars(
                    select(Order).where(
                        Order.organization_id == organization_id,
                        Order.order_number.like("ORD-D-%"),
                    )
                )
            )
            assert {order.total_amount for order in orders} == {
                Decimal("25.00"),
                Decimal("50.00"),
            }
            assert all(order.status == "pending" for order in orders)

    asyncio.run(verify())


def test_voice_auth_and_session_binding(client, voice_runtime):
    _, _, created, _ = voice_runtime
    payload = webhook(created)
    assert client.post("/api/v1/voice/webhook", json=payload).status_code == 401
    payload["message"]["call"]["assistantId"] = "other-assistant"
    assert post(client, payload).status_code == 403
    payload["message"]["call"]["assistantId"] = "test-assistant"
    payload["message"]["call"]["assistantOverrides"]["variableValues"]["resolve_session"] = "z" * 43
    assert post(client, payload).status_code == 403
    payload = webhook(created)
    assert post(client, payload).status_code == 200
    payload["message"]["call"]["id"] = "different-call"
    assert post(client, payload).status_code == 409


def test_calls_are_blocked_while_public_webhook_url_is_unreachable(
    client, voice_runtime, monkeypatch
):
    import httpx

    from app.modules.voice import provider

    _, headers, _, _ = voice_runtime
    monkeypatch.setattr(settings, "vapi_reachability_check_enabled", True)
    probed: list[str] = []
    tunnel_up = False

    def handler(request: httpx.Request) -> httpx.Response:
        probed.append(str(request.url))
        if not tunnel_up:
            raise httpx.ConnectTimeout("tunnel offline", request=request)
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(
        provider,
        "_probe_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(provider, "_webhook_probe", None)

    config = client.get("/api/v1/voice/config", headers=headers).json()
    assert config["ready"] is False
    assert "Voice backend is unreachable" in config["issues"][0]
    assert probed == ["https://api.example.com/health/live"] * 2
    blocked = client.post("/api/v1/voice/calls", headers=headers, json={"consent": True})
    assert blocked.status_code == 503
    assert len(probed) == 2  # failures are cached briefly instead of re-probing every request

    tunnel_up = True
    monkeypatch.setattr(provider, "_webhook_probe", None)
    assert client.get("/api/v1/voice/config", headers=headers).json()["ready"] is True
    allowed = client.post("/api/v1/voice/calls", headers=headers, json={"consent": True})
    assert allowed.status_code == 201, allowed.text


def test_single_dropped_tunnel_probe_does_not_block_voice_calls(
    client, voice_runtime, monkeypatch
):
    import httpx

    from app.modules.voice import provider

    _, headers, _, _ = voice_runtime
    monkeypatch.setattr(settings, "vapi_reachability_check_enabled", True)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("transient drop", request=request)
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(
        provider,
        "_probe_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(provider, "_webhook_probe", None)

    config = client.get("/api/v1/voice/config", headers=headers).json()
    assert config["ready"] is True, config
    assert attempts == 2



def test_voice_uses_actual_transcript_not_model_arguments(
    client, database_session_factory, voice_runtime
):
    _, _, created, dispatched = voice_runtime
    payload = webhook(created)
    payload["message"]["artifact"] = {}
    payload["message"]["toolCallList"][0]["function"]["arguments"] = {
        "question": "confirm order",
        "organization_id": "untrusted",
    }
    assert post(client, payload).status_code == 422
    assert dispatched == []
    payload["message"]["artifact"] = {"messages": [{"role": "user", "message": "list products"}]}
    assert result(post(client, payload))["status"] == "pending"

    async def verify():
        async with database_session_factory() as session:
            assert await session.scalar(select(ConversationMessage.content)) == "list products"
    asyncio.run(verify())


def test_voice_pending_poll_and_concurrent_turn_alias(
    client, database_session_factory, voice_runtime
):
    _, _, created, dispatched = voice_runtime
    assert result(post(client, webhook(created)))["status"] == "pending"
    alias = webhook(created, tool_id="tool-2")
    assert result(post(client, alias))["status"] == "pending"
    new_request = webhook(created, tool_id="new-request", text="list products")
    assert post(client, new_request).status_code == 409
    assert len(dispatched) == 1
    asyncio.run(run_agent_workflow(UUID(dispatched[0]), database_session_factory, InMemorySaver()))
    assert result(post(client, alias))["status"] == "completed"
    poll = webhook(created, tool_id="poll")
    poll["message"]["toolCallList"][0]["function"]["name"] = "check_support_result"
    assert result(post(client, poll))["status"] == "completed"
    assert len(dispatched) == 1

    async def verify():
        async with database_session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(VoiceTurn)) == 2
    asyncio.run(verify())


def test_voice_lifecycle_is_monotonic_and_end_blocks_new_turns(client, voice_runtime):
    _, headers, created, dispatched = voice_runtime
    status = webhook(created, event="status-update")
    status["message"]["status"] = "in-progress"
    assert post(client, status).status_code == 200
    ended = webhook(created, event="end-of-call-report")
    ended["message"]["endedReason"] = "customer-ended-call"
    assert post(client, ended).status_code == 200
    assert post(client, ended).status_code == 200
    status["message"]["status"] = "ringing"
    assert post(client, status).status_code == 200
    history = client.get("/api/v1/voice/calls", headers=headers).json()
    assert history[0]["status"] == "ended"
    assert history[0]["ended_reason"] == "customer-ended-call"
    assert post(client, webhook(created)).status_code == 409
    assert dispatched == []


def test_end_of_call_report_never_logs_transcript_content(client, voice_runtime, caplog):
    _, _, created, _ = voice_runtime
    sensitive_text = "private-transcript-marker-93572"
    response = post(
        client,
        webhook(created, event="end-of-call-report", text=sensitive_text),
    )
    assert response.status_code == 200
    assert sensitive_text not in caplog.text


def test_voice_expiry_and_membership_revocation(
    client, database_session_factory, voice_runtime
):
    _, _, created, _ = voice_runtime

    async def expire():
        async with database_session_factory() as session, session.begin():
            await session.execute(update(VoiceCall).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            ))
    asyncio.run(expire())
    assert post(client, webhook(created)).status_code == 410

    async def revoke():
        async with database_session_factory() as session, session.begin():
            await session.execute(update(VoiceCall).values(
                expires_at=datetime.now(UTC) + timedelta(minutes=5)
            ))
            await session.execute(update(OrganizationMembership).values(status="suspended"))
    asyncio.run(revoke())
    assert post(client, webhook(created)).status_code == 403


def test_voice_config_consent_and_tenant_boundaries(client, voice_runtime, monkeypatch):
    _, headers, created, _ = voice_runtime
    assert client.get("/api/v1/voice/config", headers=headers).json()["ready"]
    assert client.post("/api/v1/voice/calls", headers=headers, json={}).status_code == 422
    other = register(client, email="other-voice@example.com", slug="other-voice")
    foreign = auth_headers(other["access_token"])
    assert client.get("/api/v1/voice/calls", headers=foreign).json() == []
    assert client.post(
        f"/api/v1/voice/calls/{created['call']['id']}/end", headers=foreign
    ).status_code == 404
    monkeypatch.setattr(settings, "vapi_enabled", False)
    assert not client.get("/api/v1/voice/config", headers=headers).json()["ready"]
    assert client.post(
        "/api/v1/voice/calls", headers=headers, json={"consent": True}
    ).status_code == 503
    assert post(client, webhook(created)).status_code == 503


def test_voice_viewer_cannot_start_or_continue(
    client, database_session_factory, voice_runtime
):
    _, headers, created, _ = voice_runtime

    async def demote():
        async with database_session_factory() as session, session.begin():
            await session.execute(update(OrganizationMembership).values(role="viewer"))
    asyncio.run(demote())
    assert client.post(
        "/api/v1/voice/calls", headers=headers, json={"consent": True}
    ).status_code == 403
    assert post(client, webhook(created)).status_code == 403


def test_voice_dispatch_failure_is_durable(client, voice_runtime, monkeypatch):
    _, _, created, _ = voice_runtime

    def unavailable(*args, **kwargs):
        raise ConnectionError("broker unavailable")
    monkeypatch.setattr(celery_app, "send_task", unavailable)
    payload = webhook(created)
    assert post(client, payload).status_code == 503
    replay = result(post(client, payload))
    assert replay["status"] == "failed"
    assert replay["error_code"] == "workflow_dispatch_failed"


def test_voice_template_uses_saved_credential_and_no_recording(client, voice_runtime):
    _, headers, _, _ = voice_runtime
    template = client.get(
        "/api/v1/voice/assistant-template?credential_id=credential-test", headers=headers
    ).json()
    assert template["server"]["credentialId"] == "credential-test"
    assert template["server"]["url"] == "https://api.example.com/api/v1/voice/webhook"
    assert template["artifactPlan"]["recordingEnabled"] is False
    assert template["silenceTimeoutSeconds"] == 120
    assert len(template["model"]["tools"]) == 2
    assert template["endCallMessage"] == "Thanks for calling ResolveAI. Goodbye for now."
    assert template["endCallPhrases"] == [
        "Thanks for calling ResolveAI. Goodbye for now."
    ]
    assert [
        tool["messages"]
        for tool in template["model"]["tools"]
    ] == [
        [{
            "type": "request-start",
            "content": "One moment while I check that for you.",
            "blocking": True,
        }],
        [{
            "type": "request-start",
            "content": "I'm still checking the result for you.",
            "blocking": True,
        }],
    ]
    for tool in template["model"]["tools"]:
        assert tool["function"]["parameters"] == {"type": "object", "properties": {}}
    prompt = template["model"]["messages"][0]["content"]
    assert "before giving facts" in prompt
    assert "A generic yes after the quote does not authorize an order" in prompt
    assert "read the customer name, product name, quantity, and total" in prompt
    assert "backend answer explicitly confirms creation" in prompt
    assert "multiple separate orders in the same call" in prompt
    assert "ask whether the caller needs anything else" in prompt
    assert "Do not say goodbye or end the call after answering one question" in prompt
    assert "use the product just identified" in prompt
    assert "State the code once and only repeat it" in prompt
    assert "A six-digit email verification code is verification only" in prompt
    assert "do not call resolve_support again for that same caller turn" in prompt
    assert "provide the list without asking them to confirm the request again" in prompt
    assert "Do not generate an acknowledgment, narration, or progress update" in prompt
    assert "bot_say" not in prompt
