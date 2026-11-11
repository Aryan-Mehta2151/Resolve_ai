import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import select
from test_tools import auth_headers, register

from app.core.celery import celery_app
from app.core.config import settings
from app.db.models import (
    AgentWorkflow,
    Conversation,
    CustomerEmailVerification,
    Order,
    Product,
    ToolExecution,
)
from app.modules.agent.service import run_agent_workflow


def _configure_customer_email(
    monkeypatch,
    app_module,
    delivered: list[tuple[str, str, str]],
) -> None:
    monkeypatch.setattr(settings, "customer_email_smtp_host", "mailpit")
    monkeypatch.setattr(settings, "customer_email_smtp_starttls", False)
    monkeypatch.setattr(settings, "customer_email_verification_url", "http://localhost:3000/customer")

    async def send_verification(recipient: str, organization: str, token: str) -> None:
        delivered.append((recipient, organization, token))

    monkeypatch.setattr(app_module, "send_verification_email", send_verification)


def _provision_customer(client: TestClient, tenant: dict, email: str) -> dict:
    response = client.post(
        "/api/v1/customers",
        headers=auth_headers(tenant["access_token"]),
        json={"full_name": "Taylor Customer", "email": email},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _verification_setup(client: TestClient, monkeypatch):
    from app.modules.auth import router as auth_router

    tenant = register(client, email="portal-admin@example.com", slug="portal-store")
    customer = _provision_customer(client, tenant, "taylor@example.com")
    delivered: list[tuple[str, str, str]] = []
    _configure_customer_email(monkeypatch, auth_router, delivered)
    payload = {
        "organization_slug": "portal-store",
        "email": "TAYLOR@example.com",
        "password": "Customer-Strong-Password-1!",
    }
    return tenant, customer, payload, delivered


def test_customer_verification_is_required_and_customer_access_is_isolated(
    client: TestClient,
    database_session_factory,
    monkeypatch,
) -> None:
    _, customer, payload, delivered = _verification_setup(client, monkeypatch)
    signup = client.post("/api/v1/customer/auth/register", json=payload)

    assert signup.status_code == 202
    assert len(delivered) == 1
    assert "verification link will be sent" in signup.json()["message"]
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post(
        "/api/v1/customer/auth/verify",
        json={"token": "invalid-" + "x" * 40},
    ).status_code == 400

    verified = client.post(
        "/api/v1/customer/auth/verify",
        json={"token": delivered[0][2]},
    )
    assert verified.status_code == 200
    assert client.post(
        "/api/v1/customer/auth/verify",
        json={"token": delivered[0][2]},
    ).status_code == 400

    login = client.post("/api/v1/customer/auth/login", json=payload)
    assert login.status_code == 200, login.text
    assert login.json()["role"] == "customer"
    customer_headers = auth_headers(login.json()["access_token"])
    assert client.get("/api/v1/auth/me", headers=customer_headers).status_code == 403
    assert client.get("/api/v1/customers", headers=customer_headers).status_code == 403

    profile = client.get("/api/v1/customer/profile", headers=customer_headers)
    assert profile.status_code == 200
    assert profile.json()["id"] == customer["id"]
    assert profile.json()["full_name"] == "Taylor Customer"
    assert client.get("/api/v1/customer/orders", headers=customer_headers).json() == []

    async def verify_database_state() -> None:
        async with database_session_factory() as session:
            verification = await session.scalar(select(CustomerEmailVerification))
            assert verification is not None
            assert verification.token_hash != delivered[0][2]
            assert verification.used_at is not None

    asyncio.run(verify_database_state())


def test_customer_signup_requires_a_matching_profile_and_expired_tokens_fail(
    client: TestClient,
    database_session_factory,
    monkeypatch,
) -> None:
    from app.modules.auth import router as auth_router

    tenant = register(client, email="profile-admin@example.com", slug="profile-store")
    delivered: list[tuple[str, str, str]] = []
    _configure_customer_email(monkeypatch, auth_router, delivered)
    payload = {
        "organization_slug": "profile-store",
        "email": "missing@example.com",
        "password": "Customer-Strong-Password-1!",
    }
    missing_profile = client.post("/api/v1/customer/auth/register", json=payload)
    assert missing_profile.status_code == 202
    assert delivered == []

    customer = _provision_customer(client, tenant, "expires@example.com")
    payload["email"] = customer["email"]
    assert client.post("/api/v1/customer/auth/register", json=payload).status_code == 202
    assert len(delivered) == 1

    async def expire_token() -> None:
        async with database_session_factory() as session, session.begin():
            verification = await session.scalar(select(CustomerEmailVerification))
            assert verification is not None
            verification.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    asyncio.run(expire_token())
    expired = client.post(
        "/api/v1/customer/auth/verify",
        json={"token": delivered[0][2]},
    )
    assert expired.status_code == 400
    assert client.post(
        "/api/v1/customer/auth/login",
        json=payload,
    ).status_code == 401


def test_customer_voice_order_uses_linked_profile_and_order_history(
    client: TestClient,
    database_session_factory,
    monkeypatch,
) -> None:
    from app.modules.customer import router as customer_router

    tenant, customer, payload, delivered = _verification_setup(client, monkeypatch)
    assert client.post("/api/v1/customer/auth/register", json=payload).status_code == 202
    assert client.post(
        "/api/v1/customer/auth/verify",
        json={"token": delivered[0][2]},
    ).status_code == 200
    login = client.post("/api/v1/customer/auth/login", json=payload)
    assert login.status_code == 200, login.text
    customer_headers = auth_headers(login.json()["access_token"])

    monkeypatch.setattr(settings, "vapi_enabled", True)
    monkeypatch.setattr(settings, "vapi_public_key", "test-public-key")
    monkeypatch.setattr(settings, "vapi_assistant_id", "test-assistant")
    webhook_secret = "test-webhook-" + "x" * 32
    monkeypatch.setattr(settings, "vapi_webhook_secret", SecretStr(webhook_secret))
    monkeypatch.setattr(settings, "vapi_public_api_url", "https://api.example.com")
    monkeypatch.setattr(settings, "vapi_result_wait_seconds", 0)
    monkeypatch.setattr(settings, "agent_intent_provider", "deterministic")
    async def ready() -> list[str]:
        return []

    monkeypatch.setattr(customer_router, "voice_readiness_issues", ready)
    dispatched: list[str] = []

    def capture_dispatch(*args, **kwargs) -> None:
        if not args:
            raise AssertionError("Celery task name is required")
        dispatched.append(kwargs["args"][0])

    monkeypatch.setattr(celery_app, "send_task", capture_dispatch)

    async def add_product() -> None:
        async with database_session_factory() as session, session.begin():
            session.add(
                Product(
                    organization_id=UUID(tenant["organization"]["id"]),
                    sku="SKU-PORTAL",
                    name="Portal Product",
                    description="Customer portal test item",
                    unit_price=Decimal("12.50"),
                    stock_quantity=4,
                )
            )

    asyncio.run(add_product())
    voice_call = client.post(
        "/api/v1/customer/voice/calls",
        headers=customer_headers,
        json={"consent": True, "customer_id": "00000000-0000-0000-0000-000000000001"},
    )
    assert voice_call.status_code == 201, voice_call.text
    created = voice_call.json()
    assert created["call"]["conversation_id"]
    assert created["session_token"]

    async def verify_ownership() -> None:
        async with database_session_factory() as session:
            conversation = await session.get(
                Conversation, UUID(created["call"]["conversation_id"])
            )
            assert conversation is not None
            assert str(conversation.customer_id) == customer["id"]

    asyncio.run(verify_ownership())

    def submit_turn(tool_id: str, text: str) -> str:
        response = client.post(
            "/api/v1/voice/webhook",
            headers={"Authorization": f"Bearer {webhook_secret}"},
            json={
                "message": {
                    "type": "tool-calls",
                    "call": {
                        "id": "portal-provider-call",
                        "assistantId": "test-assistant",
                        "type": "webCall",
                        "assistantOverrides": {
                            "variableValues": {"resolve_session": created["session_token"]}
                        },
                    },
                    "toolCallList": [
                        {
                            "id": tool_id,
                            "function": {"name": "resolve_support", "arguments": {}},
                        }
                    ],
                    "artifact": {"messages": [{"role": "user", "message": text}]},
                }
            },
        )
        assert response.status_code == 200, response.text
        result = json.loads(response.json()["results"][0]["result"])
        if result["status"] == "pending":
            assert dispatched
            asyncio.run(
                run_agent_workflow(
                    UUID(dispatched[-1]),
                    database_session_factory,
                    InMemorySaver(),
                )
            )
            response = client.post(
                "/api/v1/voice/webhook",
                headers={
                    "Authorization": f"Bearer {webhook_secret}"
                },
                json={
                    "message": {
                        "type": "tool-calls",
                        "call": {
                            "id": "portal-provider-call",
                            "assistantId": "test-assistant",
                            "type": "webCall",
                            "assistantOverrides": {
                                "variableValues": {
                                    "resolve_session": created["session_token"]
                                }
                            },
                        },
                        "toolCallList": [
                            {
                                "id": tool_id,
                                "function": {"name": "resolve_support", "arguments": {}},
                            }
                        ],
                        "artifact": {"messages": [{"role": "user", "message": text}]},
                    }
                },
            )
            assert response.status_code == 200, response.text
            result = json.loads(response.json()["results"][0]["result"])
        return result["answer"]

    quote = submit_turn("customer-quote", "Buy 2 SKU-PORTAL")
    async def latest_tool_error() -> str | None:
        async with database_session_factory() as session:
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.workflow_id == UUID(dispatched[-1]))
                .order_by(ToolExecution.created_at.desc())
                .limit(1)
            )
            return None if execution is None else execution.error_code

    tool_error = asyncio.run(latest_tool_error())
    assert "Taylor Customer" in quote, f"{quote}; tool error: {tool_error}"
    assert "25.00 USD" in quote
    assert "No order has been created" in quote
    confirmation = submit_turn("customer-confirm", "Confirm order.")
    assert "Taylor Customer" in confirmation
    assert "created" in confirmation

    own_orders = client.get("/api/v1/customer/orders", headers=customer_headers)
    assert own_orders.status_code == 200
    assert len(own_orders.json()) == 1
    assert own_orders.json()[0]["customer_id"] == customer["id"]
    assert own_orders.json()[0]["order_number"] in confirmation

    async def verify_order_actor() -> None:
        async with database_session_factory() as session:
            order = await session.scalar(
                select(Order).where(
                    Order.organization_id == UUID(tenant["organization"]["id"]),
                    Order.customer_id == UUID(customer["id"]),
                )
            )
            assert order is not None
            conversation = await session.get(
                Conversation, UUID(created["call"]["conversation_id"])
            )
            assert conversation is not None
            membership = await session.scalar(
                select(AgentWorkflow.requested_by_user_id).where(
                    AgentWorkflow.conversation_id == conversation.id
                )
            )
            assert membership == UUID(login.json()["user"]["id"])

    asyncio.run(verify_order_actor())
