import re
from collections.abc import Awaitable, Callable
from typing import NotRequired, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from mcp import Client
from pydantic import ValidationError

from app.modules.agent.schemas import AgentIntent, IntentAnalysis
from app.modules.knowledge.schemas import KnowledgeAnswer
from app.modules.tools.order_confirmation import is_order_confirmation
from app.modules.tools.schemas import (
    BusinessActionOutput,
    CreateSupportTicketToolOutput,
    DemoOrderOutput,
    OrderQuoteOutput,
    OrderStatusToolOutput,
    ProductListOutput,
    QuoteOrderInput,
    RefundStatusToolOutput,
)

MAX_CONTEXT_MESSAGES = 20
MAX_GRAPH_STEPS = 8
_ORDER_NUMBER_PATTERN = re.compile(r"\bORD-[A-Za-z0-9-]{1,28}\b", re.IGNORECASE)
_SUPPORT_TICKET_REQUEST_PATTERN = re.compile(
    r"\b(?:open|create|file|raise|submit)\s+(?:a\s+)?(?:support\s+)?ticket\b",
    re.IGNORECASE,
)


class MessageState(TypedDict):
    role: str
    content: str


class AgentState(TypedDict):
    conversation_id: str
    organization_id: str
    workflow_id: str
    customer_id: NotRequired[str | None]
    call_id: NotRequired[str | None]
    messages: list[MessageState]
    latest_message: str
    intent: NotRequired[str]
    entities: NotRequired[dict[str, str]]
    response: NotRequired[str]
    source_references: NotRequired[list[dict[str, str]]]
    knowledge_answer: NotRequired[str]
    tool_results: NotRequired[list[dict[str, object]]]
    approval_status: NotRequired[str]
    escalation_required: NotRequired[bool]
    workflow_status: NotRequired[str]
    error_state: NotRequired[str]


EventRecorder = Callable[[str, dict[str, object]], Awaitable[None]]
KnowledgeAnswerer = Callable[[str], Awaitable[KnowledgeAnswer | None]]


class IntentClassifier(Protocol):
    name: str

    async def classify(
        self,
        message: str,
        conversation_context: list[MessageState] | None = None,
    ) -> IntentAnalysis: ...


class ResponseGenerator(Protocol):
    name: str

    async def generate(
        self,
        messages: list[MessageState],
        intent: AgentIntent,
        evidence: dict[str, object],
    ) -> str: ...


class DeterministicIntentClassifier:
    name = "deterministic"

    async def classify(
        self,
        message: str,
        conversation_context: list[MessageState] | None = None,
    ) -> IntentAnalysis:
        if is_catalog_affirmation(message, conversation_context):
            return IntentAnalysis(intent=AgentIntent.LIST_PRODUCTS)
        return classify_intent(message)


def is_catalog_request(message: str) -> bool:
    normalized = re.sub(r"\bs\s*\.?\s*k\s*\.?\s*u\b", "sku", message.casefold())
    if normalized.strip(" !.,?") == "what can i buy":
        return True
    if re.search(r"\b(?:cancel|refund|buy|purchase|track|shipment)\b", normalized):
        return False
    return bool(
        re.search(
            r"\b(?:list|show|read|fetch)\b.{0,60}"
            r"\b(?:skus?|products?|catalog(?:ue)?|items)\b",
            normalized,
        )
        or re.search(
            r"\b(?:give|tell)\b.{0,30}\b(?:list|available|current|all)\b.{0,30}"
            r"\b(?:skus?|products?|catalog(?:ue)?|items)\b",
            normalized,
        )
        or re.search(
            r"\b(?:what|which)\s+(?:products?|items|skus?)\s+"
            r"(?:(?:do|can)\s+you\s+(?:have|sell|offer)|are\s+available)\b",
            normalized,
        )
    )


def is_catalog_affirmation(
    message: str,
    conversation_context: list[MessageState] | None,
) -> bool:
    normalized = re.sub(r"[\s,.!?]+", " ", message.casefold()).strip()
    if normalized not in {
        "yes",
        "yeah",
        "yep",
        "sure",
        "please",
        "of course",
        "yes please",
        "yes i want it",
        "yes i want that",
        "i want it",
        "i want that",
    } or not conversation_context:
        return False

    prior_messages = conversation_context
    if (
        prior_messages[-1]["role"] == "user"
        and prior_messages[-1]["content"].strip() == message.strip()
    ):
        prior_messages = prior_messages[:-1]
    latest_assistant_message = next(
        (
            item["content"]
            for item in reversed(prior_messages)
            if item["role"] == "assistant"
        ),
        "",
    )
    if "?" not in latest_assistant_message:
        return False
    offer_patterns = (
        r"\b(?:would you like|do you want|shall i|can i|may i)\b.{0,120}"
        r"\b(?:list|show|read|give|see)\b.{0,80}"
        r"\b(?:skus?|products?|catalog(?:ue)?|items)\b",
        r"\b(?:list|show|read|give|see)\b.{0,80}"
        r"\b(?:skus?|products?|catalog(?:ue)?|items)\b.{0,120}"
        r"\b(?:would you like|do you want|shall i|can i|may i)\b",
    )
    return any(
        re.search(pattern, latest_assistant_message, re.IGNORECASE)
        for pattern in offer_patterns
    )


def classify_intent(message: str) -> IntentAnalysis:
    normalized = message.casefold()
    order_match = _ORDER_NUMBER_PATTERN.search(message)
    entities = {"order_number": order_match.group(0).upper()} if order_match else {}

    if is_catalog_request(message):
        return IntentAnalysis(intent=AgentIntent.LIST_PRODUCTS)
    if is_order_confirmation(message):
        return IntentAnalysis(
            intent=AgentIntent.PLACE_ORDER,
            entities={"confirm_order": "true"},
        )
    if re.search(
        r"\b(?:place|make|create|put in|start)\s+(?:a\s+)?(?:new\s+)?order\b"
        r"|\b(?:i want|i'd like|we need)\s+to\s+order\b",
        normalized,
    ):
        return IntentAnalysis(intent=AgentIntent.PLACE_ORDER)
    purchase = re.fullmatch(r"(?:buy|order)\s+(\d+)\s+(.+?)[.!]?", message.strip(), re.I)
    if purchase:
        return IntentAnalysis(
            intent=AgentIntent.PLACE_ORDER,
            entities={
                "quantity": purchase[1],
                "product": purchase[2],
            },
        )
    if _SUPPORT_TICKET_REQUEST_PATTERN.search(message):
        return IntentAnalysis(intent=AgentIntent.SUPPORT_TICKET, entities=entities)
    if any(term in normalized for term in ("speak to a person", "human agent", "real person")):
        return IntentAnalysis(intent=AgentIntent.HUMAN_ESCALATION, escalation_required=True)
    if any(term in normalized for term in ("refund", "money back", "charge back")):
        return IntentAnalysis(intent=AgentIntent.REFUND_STATUS, entities=entities)
    if any(term in normalized for term in ("cancel", "cancellation")):
        return IntentAnalysis(intent=AgentIntent.CANCELLATION, entities=entities)
    if any(term in normalized for term in ("track", "tracking", "shipment", "delivery")):
        return IntentAnalysis(intent=AgentIntent.SHIPMENT_TRACKING, entities=entities)
    if any(term in normalized for term in ("order", "status", "where is")):
        return IntentAnalysis(intent=AgentIntent.ORDER_STATUS, entities=entities)
    if normalized.strip(" !.,?") in {"hi", "hello", "hey", "good morning", "good afternoon"}:
        return IntentAnalysis(intent=AgentIntent.GREETING)
    return IntentAnalysis(intent=AgentIntent.GENERAL)


def _tool_response(
    tool_results: list[dict[str, object]], customer_id: str | None = None
) -> str | None:
    if not tool_results:
        return None
    tool_result = tool_results[-1]
    tool_name = tool_result.get("tool_name")
    result_status = tool_result.get("status")
    output = tool_result.get("output")
    if tool_name == "commerce_request_action":
        if result_status != "succeeded" or not isinstance(output, dict):
            return str(
                tool_result.get("message")
                or ("This demo action could not be completed with your role. Contact a supervisor.")
            )
        action = BusinessActionOutput.model_validate(output)
        if action.kind == "cancellation":
            return f"Demo order {action.order_number} was cancelled. No real payment was changed."
        if action.status == "pending":
            return (
                f"Refund request for {action.order_number} is awaiting Admin/Supervisor approval. "
                "No money has moved. Check Business actions in Orders."
            )
        return (
            f"The mock refund request for {action.order_number} is {action.status}. "
            "No real money was moved."
        )
    if isinstance(tool_name, str) and tool_name in {
        "commerce_list_products",
        "commerce_quote_demo_order",
        "commerce_confirm_demo_order",
    }:
        if result_status != "succeeded" or not isinstance(output, dict):
            return str(
                tool_result.get("message")
                or (
                    "I couldn't complete that demo order step. Check your customer, role, "
                    "product and quantity, then try again. No new order was confirmed."
                )
            )
        if tool_name == "commerce_list_products":
            catalog = ProductListOutput.model_validate(output)
            if not catalog.products:
                return (
                    "There are no active demo products yet. "
                    "Ask an Admin to seed demo commerce data."
                )
            return (
                "Demo products (USD):\n"
                + "\n".join(
                    f"- {p.name} | {p.sku} | ${p.unit_price:.2f} | {p.stock_quantity} in stock"
                    for p in catalog.products
                )
                + (
                    '\n\nTo order, say "Buy 2 <SKU>". No real payment is charged.'
                    if customer_id is not None
                    else "\n\nA customer must be selected before an order can be placed."
                )
            )
        if tool_name == "commerce_quote_demo_order":
            quote = OrderQuoteOutput.model_validate(output)
            return (
                f"Demo order summary for {quote.customer_name or 'the attached customer'}: "
                f"{quote.quantity} x {quote.name} ({quote.sku}) "
                f"at {quote.unit_price:.2f} USD each. Total: {quote.total_amount:.2f} USD.\n"
                "No real payment will be charged. No order has been created yet.\n"
                'To confirm within 15 minutes, say "confirm order".'
            )
        order = DemoOrderOutput.model_validate(output)
        return (
            f"Demo order {order.order_number} for {order.customer_name} created: "
            f"{order.total_amount:.2f} "
            f"{order.currency}, status {order.status}. No real payment was charged. "
            "It is now available in Orders."
        )
    if result_status != "succeeded" or not isinstance(output, dict):
        if result_status == "unavailable":
            if tool_name == "support_create_ticket":
                return (
                    "I can't create a ticket with the current role. A support specialist can help."
                )
            return (
                "I can't access that information with the current role. "
                "A support specialist can help."
            )
        if tool_name == "support_create_ticket":
            return "I couldn't create a support ticket. A support specialist can help."
        if tool_name == "commerce_get_refund_status":
            return "I couldn't retrieve the refund status. A support specialist can help."
        return (
            "I couldn't retrieve that order status. "
            "Please check the order number or contact support."
        )

    try:
        if tool_name == "commerce_get_order_status":
            order = OrderStatusToolOutput.model_validate(output)
            response = f"Order {order.order_number} is currently {order.status}."
            if order.shipments:
                shipment = order.shipments[-1]
                response += f" The {shipment.carrier} shipment is {shipment.status}."
                if shipment.estimated_delivery_at is not None:
                    response += (
                        " The estimated delivery date is "
                        f"{shipment.estimated_delivery_at.date().isoformat()}."
                    )
            return response
        if tool_name == "commerce_get_refund_status":
            refund = RefundStatusToolOutput.model_validate(output)
            if not refund.refunds:
                return f"No refund request is recorded for order {refund.order_number}."
            statuses = ", ".join(item.status.value for item in refund.refunds)
            return f"The refund status for order {refund.order_number} is {statuses}."
        if tool_name == "support_create_ticket":
            ticket = CreateSupportTicketToolOutput.model_validate(output)
            return (
                f"I've created support ticket {ticket.ticket_number} "
                f"with status {ticket.status.value}."
            )
    except ValidationError as exc:
        raise RuntimeError("MCP tool returned an invalid structured result") from exc
    return None


def _compose_response(
    analysis: IntentAnalysis,
    tool_results: list[dict[str, object]],
    customer_id: str | None = None,
) -> str:
    tool_response = _tool_response(tool_results, customer_id)
    if tool_response is not None:
        return tool_response
    if analysis.escalation_required:
        return "I've flagged this conversation for a support specialist."
    if analysis.intent == AgentIntent.PLACE_ORDER:
        if customer_id is None:
            return (
                "A customer must be linked before I can prepare this order. "
                "Select one before starting the call or conversation; then I need the product "
                "and quantity."
            )
        product = analysis.entities.get("product")
        quantity = analysis.entities.get("quantity")
        if product is None and quantity is None:
            return (
                "Which product would you like, and how many? "
                "You can also ask me to list products."
            )
        if product is None:
            return f"Which product would you like? I have the quantity as {quantity}."
        if quantity is None:
            return f"How many of {product} would you like?"
        return (
            "I couldn't prepare that order summary. "
            "Please check the product and quantity and try again."
        )
    if analysis.intent in {AgentIntent.ORDER_STATUS, AgentIntent.SHIPMENT_TRACKING}:
        if "order_number" not in analysis.entities:
            return "I can help check an order or delivery. What is your order number?"
        return (
            f"I've noted order {analysis.entities['order_number']}. "
            "A support workflow can check its latest status."
        )
    if analysis.intent == AgentIntent.REFUND_STATUS:
        return (
            "I can help review a refund request. "
            "Refund actions require the approved support workflow."
        )
    if analysis.intent == AgentIntent.CANCELLATION:
        number = analysis.entities.get("order_number")
        return (
            (
                f'To confirm a mock cancellation, say "confirm cancel {number}". '
                "Only unpaid demo chat orders that are pending/processing "
                "with no shipment qualify. "
                "No changes have been made."
            )
            if number
            else "Which order would you like to cancel? Please provide the order number."
        )
    if analysis.intent == AgentIntent.SUPPORT_TICKET:
        return 'If you want a ticket, ask me to "create a support ticket."'
    if analysis.intent == AgentIntent.GREETING:
        return "Hello! How can I help with your order today?"
    return "I'm ready to help. Please describe the order or support issue you need help with."


def build_agent_graph(
    checkpointer: BaseCheckpointSaver,
    record_event: EventRecorder,
    classifier: IntentClassifier | None = None,
    tool_client: Client | None = None,
    available_tool_names: frozenset[str] = frozenset(),
    knowledge_answerer: KnowledgeAnswerer | None = None,
    response_generator: ResponseGenerator | None = None,
):
    intent_classifier = classifier or DeterministicIntentClassifier()

    async def classify_node(state: AgentState) -> dict[str, object]:
        message = state["latest_message"]
        confirmation = is_order_confirmation(message)
        action_request = re.fullmatch(
            r"(confirm cancel|request refund)\s+(ORD-[A-Za-z0-9-]{1,28})",
            message.strip(),
            re.I,
        )
        if action_request:
            analysis = IntentAnalysis(
                intent=(
                    AgentIntent.CANCELLATION
                    if action_request[1].lower() == "confirm cancel"
                    else AgentIntent.REFUND_STATUS
                ),
                entities={"order_number": action_request[2].upper()},
            )
        elif confirmation:
            analysis = IntentAnalysis(
                intent=AgentIntent.PLACE_ORDER, entities={"confirm_order": "true"}
            )
        else:
            analysis = await intent_classifier.classify(message, state.get("messages", []))
        await record_event(
            "intent.classified",
            {
                "intent": analysis.intent.value,
                "entities": analysis.entities,
                "escalation_required": analysis.escalation_required,
                "classifier": intent_classifier.name,
            },
        )
        return {
            "intent": analysis.intent.value,
            "entities": analysis.entities,
            "escalation_required": analysis.escalation_required,
        }

    async def execute_tool_node(state: AgentState) -> dict[str, list[dict[str, object]]]:
        if tool_client is None:
            return {"tool_results": []}

        intent = AgentIntent(state["intent"])
        order_number = state.get("entities", {}).get("order_number")
        tool_name: str | None = None
        arguments: dict[str, object] = {}
        entities = state.get("entities", {})
        action_request = re.fullmatch(
            r"(confirm cancel|request refund)\s+(ORD-[A-Za-z0-9-]{1,28})",
            state["latest_message"].strip(),
            re.I,
        )
        if action_request:
            tool_name = "commerce_request_action"
            arguments = {
                "order_number": action_request[2].upper(),
                "kind": (
                    "cancellation" if action_request[1].lower() == "confirm cancel" else "refund"
                ),
            }
        elif intent == AgentIntent.LIST_PRODUCTS:
            tool_name = "commerce_list_products"
        elif intent == AgentIntent.PLACE_ORDER:
            if is_order_confirmation(state["latest_message"]):
                tool_name = "commerce_confirm_demo_order"
            elif entities.get("product") and entities.get("quantity"):
                tool_name = "commerce_quote_demo_order"
                arguments = {"product": entities["product"], "quantity": entities["quantity"]}
                try:
                    arguments = QuoteOrderInput.model_validate(arguments).model_dump()
                except ValidationError:
                    await record_event(
                        "tool.skipped",
                        {
                            "tool_name": tool_name,
                            "reason": "invalid_arguments",
                        },
                    )
                    return {
                        "tool_results": [
                            {
                                "tool_name": tool_name,
                                "status": "failed",
                                "message": (
                                    "Choose a product and quantity from 1 to 100. "
                                    "No new order was confirmed."
                                ),
                            }
                        ]
                    }
        elif intent in {AgentIntent.ORDER_STATUS, AgentIntent.SHIPMENT_TRACKING} and order_number:
            tool_name = "commerce_get_order_status"
            arguments = {"order_number": order_number}
        elif intent == AgentIntent.REFUND_STATUS and order_number:
            tool_name = "commerce_get_refund_status"
            arguments = {"order_number": order_number}
        elif intent == AgentIntent.SUPPORT_TICKET and _SUPPORT_TICKET_REQUEST_PATTERN.search(
            state["latest_message"]
        ):
            tool_name = "support_create_ticket"
            arguments = {
                "subject": "Customer support request",
                "description": state["latest_message"],
                "priority": "normal",
                "order_number": order_number,
            }
        if tool_name is None:
            return {"tool_results": []}
        if tool_name not in available_tool_names:
            await record_event(
                "tool.skipped",
                {"tool_name": tool_name, "reason": "permission_denied"},
            )
            return {"tool_results": [{"tool_name": tool_name, "status": "unavailable"}]}

        result = await tool_client.call_tool(tool_name, arguments)
        if result.is_error:
            message = " ".join(item.text for item in result.content if item.type == "text")
            return {
                "tool_results": [
                    {
                        "tool_name": tool_name,
                        "status": "failed",
                        "message": message,
                    }
                ]
            }
        structured_content = result.structured_content
        if not isinstance(structured_content, dict):
            raise RuntimeError("MCP tool returned no structured content")
        return {
            "tool_results": [
                {
                    "tool_name": tool_name,
                    "status": "succeeded",
                    "output": structured_content,
                }
            ]
        }

    async def retrieve_knowledge_node(state: AgentState) -> dict[str, object]:
        empty_knowledge = {"knowledge_answer": "", "source_references": []}
        if knowledge_answerer is None or state.get("intent") != AgentIntent.GENERAL.value:
            return empty_knowledge
        answer = await knowledge_answerer(state["latest_message"])
        if answer is None:
            return empty_knowledge
        if not answer.citations:
            await record_event(
                "knowledge.no_grounded_sources",
                {"intent": AgentIntent.GENERAL.value},
            )
            return {"knowledge_answer": answer.answer, "source_references": []}

        references = [
            {
                "document_id": str(citation.document_id),
                "version_id": str(citation.version_id),
                "title": citation.title,
                "filename": citation.filename,
            }
            for citation in answer.citations
        ]
        await record_event(
            "knowledge.retrieved",
            {
                "citation_count": len(references),
                "source_references": references,
            },
        )
        return {"knowledge_answer": answer.answer, "source_references": references}

    async def response_node(state: AgentState) -> dict[str, str]:
        analysis = IntentAnalysis(
            intent=AgentIntent(state["intent"]),
            entities=state.get("entities", {}),
            escalation_required=state.get("escalation_required", False),
        )
        source_references = state.get("source_references", [])
        knowledge_answer = state.get("knowledge_answer")
        has_grounded_knowledge = bool(knowledge_answer and source_references)
        evidence: dict[str, object] = {
            "draft_response": _compose_response(
                analysis,
                state.get("tool_results", []),
                state.get("customer_id"),
            ),
            "tool_results": state.get("tool_results", []),
        }
        if has_grounded_knowledge:
            evidence["approved_knowledge"] = knowledge_answer
            evidence["knowledge_sources"] = source_references

        server_composed = analysis.intent in {
            AgentIntent.PLACE_ORDER,
            AgentIntent.LIST_PRODUCTS,
            AgentIntent.CANCELLATION,
        } or any(
            r.get("tool_name") == "commerce_request_action" for r in state.get("tool_results", [])
        )
        if server_composed:
            response = str(evidence["draft_response"])
        elif response_generator is not None:
            response = await response_generator.generate(
                state["messages"],
                analysis.intent,
                evidence,
            )
            if not response.strip():
                raise RuntimeError("Response generator returned an empty response")
            response = response.strip()
        elif has_grounded_knowledge and knowledge_answer is not None:
            response = knowledge_answer
        else:
            response = str(evidence["draft_response"])

        if has_grounded_knowledge:
            citations = "; ".join(
                f"[{index}] {reference['title']} ({reference['filename']})"
                for index, reference in enumerate(source_references, start=1)
            )
            response = f"{response}\n\nSources: {citations}"
        await record_event(
            "response.prepared",
            {
                "intent": analysis.intent.value,
                "grounded": bool(source_references),
                "generator": (
                    response_generator.name
                    if response_generator is not None and not server_composed
                    else "deterministic"
                ),
            },
        )
        return {"response": response}

    graph = StateGraph(AgentState)
    graph.add_node("classify_intent", classify_node)
    graph.add_node("execute_tool", execute_tool_node)
    graph.add_node("retrieve_knowledge", retrieve_knowledge_node)
    graph.add_node("prepare_response", response_node)
    graph.add_edge(START, "classify_intent")
    graph.add_edge("classify_intent", "execute_tool")
    graph.add_edge("execute_tool", "retrieve_knowledge")
    graph.add_edge("retrieve_knowledge", "prepare_response")
    graph.add_edge("prepare_response", END)
    return graph.compile(checkpointer=checkpointer)
