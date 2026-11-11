import asyncio
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.celery import AGENT_WORKFLOW_TASK_NAME, celery_app
from app.core.config import settings
from app.db.models import (
    AgentWorkflow,
    AgentWorkflowStatus,
    ConversationMessage,
    ConversationMessageRole,
    WorkflowEvent,
)
from app.modules.agent import provider
from app.modules.agent.graph import (
    AgentState,
    DeterministicIntentClassifier,
    build_agent_graph,
    classify_intent,
    is_catalog_affirmation,
)
from app.modules.agent.provider import (
    ClassifiedSupportIntent,
    GeneratedAssistantResponse,
    OpenAIIntentClassifier,
    OpenAIResponseGenerator,
    get_intent_classifier,
    get_response_generator,
)
from app.modules.agent.schemas import AgentIntent
from app.modules.agent.service import run_agent_workflow


def register(
    client: TestClient,
    *,
    email: str,
    slug: str,
) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Agent Test",
            "organization_slug": slug,
            "full_name": "Agent Admin",
            "email": email,
            "password": "Correct-Horse-Battery-1!",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def test_classifier_returns_structured_intent_and_order_entity() -> None:
    analysis = classify_intent("Where is my order ord-10045?")

    assert analysis.intent == AgentIntent.ORDER_STATUS
    assert analysis.entities == {"order_number": "ORD-10045"}
    assert classify_intent("Please let me speak to a human agent").escalation_required
    assert classify_intent("Hello!").intent == AgentIntent.GREETING


@pytest.mark.parametrize("message", ["confirm order", "Confirm order.", "CONFIRM ORDER!"])
def test_classifier_accepts_punctuated_order_confirmation(message: str) -> None:
    analysis = classify_intent(message)

    assert analysis.intent == AgentIntent.PLACE_ORDER
    assert analysis.entities == {"confirm_order": "true"}


def test_classifier_recognizes_new_order_request_without_order_number() -> None:
    analysis = classify_intent("I want to place a new order.")

    assert analysis.intent == AgentIntent.PLACE_ORDER
    assert analysis.entities == {}


def test_openai_classifier_receives_context_for_missing_order_details(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeModel:
        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, messages):
            captured["system"] = messages[0].content
            captured["user"] = json.loads(messages[1].content)
            return ClassifiedSupportIntent(
                intent=AgentIntent.PLACE_ORDER,
                order_number=None,
                escalation_required=False,
                product="AUDIO-101",
                quantity=2,
            )

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", lambda **kwargs: FakeModel())
    context = [
        {"role": "user", "content": "I want to order a cable."},
        {"role": "assistant", "content": "How many USB-C Audio Cables would you like?"},
        {"role": "user", "content": "Two, please."},
    ]

    result = asyncio.run(
        OpenAIIntentClassifier().classify("Two, please.", conversation_context=context)
    )

    assert result.intent == AgentIntent.PLACE_ORDER
    assert result.entities == {"product": "AUDIO-101", "quantity": "2"}
    assert captured["user"] == {
        "recent_conversation": context[:-1],
        "latest_customer_message": "Two, please.",
    }
    assert "Do not guess or default a missing quantity" in str(captured["system"])


@pytest.mark.parametrize(
    ("assistant_message", "latest_message", "model_intent", "quantity", "expected_entities"),
    [
        (
            "The product code for Studio Wireless Headphones is AUDIO-100. "
            "Would you like to order this product? If so, tell me the quantity.",
            "Yes, give me 5 of those.",
            AgentIntent.PLACE_ORDER,
            5,
            {"product": "AUDIO-100", "quantity": "5"},
        ),
        (
            "I have the quantity as 5. Confirm the product code, is it AUDIO-100 "
            "for Studio Wireless Headphones?",
            "Yes.",
            AgentIntent.GENERAL,
            None,
            {"product": "AUDIO-100"},
        ),
        (
            "The products are AUDIO-100 and AUDIO-101. Which one would you like?",
            "Yes, give me 5 of those.",
            AgentIntent.PLACE_ORDER,
            5,
            {"quantity": "5"},
        ),
    ],
)
def test_openai_classifier_resolves_only_unambiguous_product_references(
    monkeypatch,
    assistant_message,
    latest_message,
    model_intent,
    quantity,
    expected_entities,
) -> None:
    class FakeModel:
        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, messages):
            return ClassifiedSupportIntent(
                intent=model_intent,
                order_number=None,
                escalation_required=False,
                product=None,
                quantity=quantity,
            )

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", lambda **kwargs: FakeModel())
    context = [
        {"role": "assistant", "content": assistant_message},
        {"role": "user", "content": latest_message},
    ]

    result = asyncio.run(
        OpenAIIntentClassifier().classify(latest_message, conversation_context=context)
    )

    assert result.intent == (
        AgentIntent.PLACE_ORDER
        if expected_entities.get("product")
        else model_intent
    )
    assert result.entities == expected_entities


@pytest.mark.parametrize(
    ("entities", "expected"),
    [
        ({}, "Which product would you like, and how many?"),
        ({"product": "AUDIO-101"}, "How many of AUDIO-101 would you like?"),
        ({"quantity": "2"}, "Which product would you like? I have the quantity as 2."),
    ],
)
def test_incomplete_order_asks_only_for_missing_details(entities, expected) -> None:
    from app.modules.agent.graph import _compose_response
    from app.modules.agent.schemas import IntentAnalysis

    response = _compose_response(
        IntentAnalysis(intent=AgentIntent.PLACE_ORDER, entities=entities),
        [],
        customer_id="customer-1",
    )

    assert response.startswith(expected)
    assert "order number" not in response.lower()


@pytest.mark.parametrize("message", [
    "Can you list the SKUs?",
    "Please show me the list of S K U codes.",
    "Read me the current products.",
    "Could you give me all the SKUs?",
    "what can i buy?",
    "Hi, I wanted to know what products do you have.",
    "What products are available?",
    "Which items do you sell?",
])
def test_catalog_requests_include_spoken_sku_lists(message):
    assert classify_intent(message).intent == AgentIntent.LIST_PRODUCTS


@pytest.mark.parametrize("message", [
    "What does SKU mean?",
    "Tell me what SKU means.",
    "Buy 2 AUDIO-101",
    "Cancel my order and show products",
    "What products do you have and cancel my order",
    "What products do you have? Buy 2 AUDIO-101",
    "What does product availability mean?",
])
def test_catalog_request_does_not_replace_other_intents(message):
    assert classify_intent(message).intent != AgentIntent.LIST_PRODUCTS


def test_openai_provider_requires_explicit_api_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_intent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", None)

    with pytest.raises(RuntimeError, match="requires OPENAI_API_KEY"):
        get_intent_classifier()


def test_openai_provider_builds_structured_classifier(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_intent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))

    classifier = get_intent_classifier()

    assert isinstance(classifier, OpenAIIntentClassifier)


@pytest.mark.parametrize("message", [
    "Can you list the SKUs?",
    "Hi, I wanted to know what products do you have.",
])
def test_openai_catalog_route_does_not_depend_on_model_classification(monkeypatch, message):
    clients = []

    class FakeModel:
        def __init__(self, **kwargs):
            clients.append(kwargs["http_async_client"])

        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, messages):
            pytest.fail("An explicit catalog request must not need model classification")

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", FakeModel)
    result = asyncio.run(OpenAIIntentClassifier().classify(message))
    assert result.intent == AgentIntent.LIST_PRODUCTS
    assert clients[0].is_closed


@pytest.mark.parametrize("message", ["Yes.", "Yes, I want it."])
def test_catalog_affirmation_uses_the_previous_assistant_offer(monkeypatch, message):
    class FakeModel:
        def __init__(self, **kwargs):
            self.client = kwargs["http_async_client"]

        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, messages):
            pytest.fail("A direct yes to a catalog offer must not need model classification")

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", FakeModel)
    context = [
        {
            "role": "assistant",
            "content": "Would you like me to list the available products and their product codes?",
        },
        {"role": "user", "content": message},
    ]

    openai_result = asyncio.run(
        OpenAIIntentClassifier().classify(message, conversation_context=context)
    )
    deterministic_result = asyncio.run(
        DeterministicIntentClassifier().classify(message, conversation_context=context)
    )

    assert openai_result.intent == AgentIntent.LIST_PRODUCTS
    assert deterministic_result.intent == AgentIntent.LIST_PRODUCTS
    assert not is_catalog_affirmation(
        message,
        [
            {"role": "assistant", "content": "Would you like me to check the order status?"},
            {"role": "user", "content": message},
        ],
    )


@pytest.mark.parametrize("operation", ["classify", "generate"])
def test_openai_calls_enforce_total_deadline(monkeypatch, operation):
    clients = []

    class SlowModel:
        def __init__(self, **kwargs):
            clients.append(kwargs["http_async_client"])

        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, messages):
            await asyncio.sleep(1)
            pytest.fail("The overall model deadline must interrupt a stalled invocation")

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(settings, "agent_model_timeout_seconds", 0.02)
    monkeypatch.setattr(provider, "ChatOpenAI", SlowModel)

    async def invoke():
        if operation == "classify":
            await OpenAIIntentClassifier().classify("Where is my order?")
        else:
            await OpenAIResponseGenerator().generate([], AgentIntent.GENERAL, {})

    with pytest.raises(TimeoutError):
        asyncio.run(invoke())
    assert clients[0].is_closed


@pytest.mark.parametrize("order_number", [None, "ORD-10045"])
def test_openai_classifier_uses_closed_schema_and_maps_entities(monkeypatch, order_number) -> None:
    class FakeModel:
        def with_structured_output(self, schema):
            assert schema is ClassifiedSupportIntent
            parameters = convert_to_openai_function(schema, strict=True)["parameters"]
            assert set(parameters["required"]) == set(parameters["properties"])
            assert parameters["additionalProperties"] is False
            assert "entities" not in parameters["properties"]
            return self

        async def ainvoke(self, messages):
            return ClassifiedSupportIntent(
                intent=AgentIntent.ORDER_STATUS,
                order_number=order_number,
                escalation_required=False,
            )

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", lambda **kwargs: FakeModel())
    result = asyncio.run(OpenAIIntentClassifier().classify("Where is my order?"))
    assert result.intent == AgentIntent.ORDER_STATUS
    assert result.entities == ({"order_number": order_number} if order_number else {})
    assert result.escalation_required is False


def test_openai_response_provider_requires_and_uses_explicit_api_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_intent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", None)
    with pytest.raises(RuntimeError, match="requires OPENAI_API_KEY"):
        get_response_generator()

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    assert isinstance(get_response_generator(), OpenAIResponseGenerator)


@pytest.mark.parametrize("fail", [False, True])
def test_openai_clients_are_task_scoped_and_closed_across_event_loops(monkeypatch, fail) -> None:
    clients = []

    class FakeModel:
        def __init__(self, **kwargs):
            clients.append(kwargs["http_async_client"])

        def with_structured_output(self, schema):
            self.schema = schema
            return self

        async def ainvoke(self, messages):
            assert not clients[-1].is_closed
            if fail:
                raise RuntimeError("provider failed")
            if self.schema is ClassifiedSupportIntent:
                return ClassifiedSupportIntent(
                    intent=AgentIntent.GREETING, order_number=None, escalation_required=False
                )
            return GeneratedAssistantResponse(response="Hello!")

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", FakeModel)

    async def turn():
        classifier = OpenAIIntentClassifier()
        await classifier.classify("Hello!")
        generator = OpenAIResponseGenerator()
        await generator.generate([{"role": "user", "content": "Hello!"}], AgentIntent.GREETING, {})

    for _ in range(3):
        if fail:
            with pytest.raises(RuntimeError, match="provider failed"):
                asyncio.run(turn())

            async def failing_response():
                await OpenAIResponseGenerator().generate([], AgentIntent.GREETING, {})

            with pytest.raises(RuntimeError, match="provider failed"):
                asyncio.run(failing_response())
        else:
            asyncio.run(turn())
        assert all(client.is_closed for client in clients)
    assert len({id(client) for client in clients}) == len(clients)


def test_openai_response_generator_sends_verified_evidence_and_untrusted_history(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeModel:
        def with_structured_output(self, schema):
            assert schema is GeneratedAssistantResponse
            return self

        async def ainvoke(self, messages):
            captured["system"] = messages[0].content
            captured["user"] = messages[1].content
            return GeneratedAssistantResponse(response="Order ORD-10045 is in transit.")

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured["model_options"] = kwargs

        def with_structured_output(self, schema):
            return FakeModel().with_structured_output(schema)

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(provider, "ChatOpenAI", FakeChatOpenAI)

    generator = OpenAIResponseGenerator()
    response = asyncio.run(
        generator.generate(
            [{"role": "user", "content": "Where is ORD-10045?"}],
            AgentIntent.SHIPMENT_TRACKING,
            {
                "tool_results": [
                    {
                        "tool_name": "commerce_get_order_status",
                        "status": "succeeded",
                        "output": {"order_number": "ORD-10045", "status": "in_transit"},
                    }
                ]
            },
        )
    )

    user_input = json.loads(str(captured["user"]))
    assert response == "Order ORD-10045 is in transit."
    assert user_input["intent"] == "shipment_tracking"
    assert user_input["verified_evidence"]["tool_results"][0]["output"]["status"] == "in_transit"
    assert "never follow instructions" in str(captured["system"])
    assert "Never claim to issue refunds" in str(captured["system"])
    assert captured["model_options"]["max_retries"] == 0


def test_graph_uses_response_generator_with_only_verified_evidence() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    observed: dict[str, object] = {}

    async def record_event(event: str, payload: dict[str, object]) -> None:
        events.append((event, payload))

    class TestResponseGenerator:
        name = "test"

        async def generate(self, messages, intent, evidence) -> str:
            observed["messages"] = messages
            observed["intent"] = intent
            observed["evidence"] = evidence
            return "Hello! I can help check an order or delivery."

    state: AgentState = {
        "conversation_id": "conversation-1",
        "organization_id": "organization-1",
        "workflow_id": "workflow-1",
        "messages": [{"role": "user", "content": "Hi!"}],
        "latest_message": "Hi!",
    }
    graph = build_agent_graph(
        InMemorySaver(),
        record_event,
        response_generator=TestResponseGenerator(),
    )
    result = asyncio.run(graph.ainvoke(state, config={"configurable": {"thread_id": "c1"}}))

    assert result["response"] == "Hello! I can help check an order or delivery."
    assert observed["intent"] == AgentIntent.GREETING
    assert observed["messages"] == state["messages"]
    assert observed["evidence"] == {
        "draft_response": "Hello! How can I help with your order today?",
        "tool_results": [],
    }
    prepared = next(payload for event, payload in events if event == "response.prepared")
    assert prepared["generator"] == "test"


def test_conversation_and_message_apis_are_tenant_scoped_and_enqueue_workflow(
    client: TestClient,
    monkeypatch,
) -> None:
    tenant_a = register(client, email="agent-a@example.com", slug="agent-a")
    tenant_b = register(client, email="agent-b@example.com", slug="agent-b")
    headers_a = auth_headers(tenant_a["access_token"])
    queued_ids: list[str] = []
    sent_task_names: list[str] = []

    def capture_task(name: str, args: list[str]) -> None:
        sent_task_names.append(name)
        queued_ids.append(args[0])

    monkeypatch.setattr(celery_app, "send_task", capture_task)
    created = client.post("/api/v1/conversations", headers=headers_a, json={})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers_a,
        json={"content": "Where is my order ORD-10045?"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "queued"
    assert queued_ids == [accepted.json()["id"]]
    assert sent_task_names == [AGENT_WORKFLOW_TASK_NAME]
    concurrent_turn = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers_a,
        json={"content": "A second message before the workflow finishes"},
    )
    assert concurrent_turn.status_code == 409

    detail = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers_a)
    assert detail.status_code == 200
    assert [message["role"] for message in detail.json()["messages"]] == ["user"]

    workflow = client.get(f"/api/v1/workflows/{accepted.json()['id']}", headers=headers_a)
    assert workflow.status_code == 200
    assert workflow.json()["events"][0]["event_type"] == "workflow.queued"

    headers_b = auth_headers(tenant_b["access_token"])
    assert (
        client.get(f"/api/v1/conversations/{conversation_id}", headers=headers_b).status_code == 404
    )
    assert (
        client.get(f"/api/v1/workflows/{accepted.json()['id']}", headers=headers_b).status_code
        == 404
    )

    blank_message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers_a,
        json={"content": "  "},
    )
    assert blank_message.status_code == 422


def test_queue_failure_is_reported_and_persisted(client: TestClient, monkeypatch) -> None:
    tenant = register(client, email="agent-failure@example.com", slug="agent-failure")
    headers = auth_headers(tenant["access_token"])
    conversation = client.post("/api/v1/conversations", headers=headers, json={}).json()

    def fail_to_queue(name: str, args: list[str]) -> None:
        assert name == AGENT_WORKFLOW_TASK_NAME
        assert args
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(celery_app, "send_task", fail_to_queue)
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": "Hello"},
    )
    assert response.status_code == 503
    workflow_id = response.json()["detail"]["workflow_id"]
    workflow = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert workflow.status_code == 200
    assert workflow.json()["status"] == AgentWorkflowStatus.FAILED.value
    assert workflow.json()["error_code"] == "workflow_dispatch_failed"
    assert workflow.json()["events"][-1]["event_type"] == "workflow.failed"


def test_worker_executes_graph_persists_events_and_is_idempotent(
    client: TestClient,
    database_session_factory: async_sessionmaker,
    monkeypatch,
) -> None:
    tenant = register(client, email="agent-worker@example.com", slug="agent-worker")
    headers = auth_headers(tenant["access_token"])

    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args: name == AGENT_WORKFLOW_TASK_NAME and bool(args),
    )
    conversation = client.post("/api/v1/conversations", headers=headers, json={}).json()
    accepted = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": "Please let me speak to a human agent."},
    )
    assert accepted.status_code == 202, accepted.text
    workflow_id = UUID(accepted.json()["id"])

    async def execute_twice() -> None:
        checkpointer = InMemorySaver()
        await run_agent_workflow(workflow_id, database_session_factory, checkpointer)
        await run_agent_workflow(workflow_id, database_session_factory, checkpointer)

    asyncio.run(execute_twice())

    workflow_detail = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert workflow_detail.status_code == 200
    assert workflow_detail.json()["status"] == AgentWorkflowStatus.COMPLETED.value
    event_types = [event["event_type"] for event in workflow_detail.json()["events"]]
    assert event_types == [
        "workflow.queued",
        "workflow.started",
        "intent.classified",
        "response.prepared",
        "workflow.completed",
    ]
    assert workflow_detail.json()["events"][2]["payload"]["classifier"] == "deterministic"

    conversation_detail = client.get(
        f"/api/v1/conversations/{conversation['id']}",
        headers=headers,
    )
    assert conversation_detail.status_code == 200
    assert conversation_detail.json()["status"] == "escalated"
    assert [message["role"] for message in conversation_detail.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert conversation_detail.json()["messages"][-1]["content"] == (
        "I've flagged this conversation for a support specialist."
    )

    async def verify_database() -> None:
        async with database_session_factory() as session:
            messages = await session.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_id == UUID(conversation["id"]),
                    ConversationMessage.role == ConversationMessageRole.ASSISTANT.value,
                )
            )
            assert len(list(messages)) == 1
            workflow = await session.get(AgentWorkflow, workflow_id)
            assert workflow is not None
            assert workflow.status == AgentWorkflowStatus.COMPLETED.value
            events = await session.scalars(
                select(WorkflowEvent).where(WorkflowEvent.workflow_id == workflow_id)
            )
            assert len(list(events)) == 5

    asyncio.run(verify_database())
