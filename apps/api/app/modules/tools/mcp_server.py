from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.db.models import MembershipRole, SupportTicketPriority
from app.modules.tools.catalog import (
    ToolCategory,
    ToolExecutionContext,
    ToolFailure,
    ToolOperation,
    tool_catalog,
)
from app.modules.tools.schemas import (
    BusinessActionOutput,
    CreateSupportTicketToolOutput,
    DemoOrderOutput,
    OrderQuoteOutput,
    OrderStatusToolOutput,
    ProductListOutput,
    RefundStatusToolOutput,
)

OrderNumberArgument = Annotated[
    str,
    Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9-]+$"),
]


async def _invoke[OutputT: BaseModel](
    tool_name: str,
    arguments: dict[str, object],
    output_model: type[OutputT],
    context: ToolExecutionContext,
) -> OutputT:
    try:
        result = await tool_catalog.invoke(tool_name, arguments, context)
    except ToolFailure as exc:
        raise ToolError(exc.public_message) from exc
    return output_model.model_validate(result.model_dump())


def build_mcp_server(context: ToolExecutionContext) -> MCPServer:
    server = MCPServer(
        name="ResolveAI",
        title="ResolveAI Support Tools",
        instructions=(
            "Tools operate only on the authenticated user's active organization. "
            "Use read tools for factual order/refund status. Write tools are permission "
            "checked and idempotent. Never claim success unless the tool confirms it."
        ),
        log_level="WARNING",
    )

    @server.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def commerce_list_products() -> ProductListOutput:
        return await _invoke("commerce_list_products", {}, ProductListOutput, context)

    if context.role in {
        MembershipRole.ADMIN,
        MembershipRole.SUPERVISOR,
        MembershipRole.SUPPORT_AGENT,
    }:
        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def commerce_request_action(
            order_number: OrderNumberArgument,
            kind: Literal["cancellation", "refund"],
        ) -> BusinessActionOutput:
            return await _invoke(
                "commerce_request_action",
                {"order_number": order_number, "kind": kind},
                BusinessActionOutput,
                context,
            )

    if context.role in {
        MembershipRole.ADMIN,
        MembershipRole.SUPERVISOR,
        MembershipRole.SUPPORT_AGENT,
        MembershipRole.CUSTOMER,
    }:
        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def commerce_quote_demo_order(
            product: Annotated[str, Field(min_length=1, max_length=160)],
            quantity: Annotated[int, Field(ge=1, le=100)],
        ) -> OrderQuoteOutput:
            return await _invoke(
                "commerce_quote_demo_order",
                {"product": product, "quantity": quantity},
                OrderQuoteOutput,
                context,
            )

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def commerce_confirm_demo_order() -> DemoOrderOutput:
            return await _invoke(
                "commerce_confirm_demo_order",
                {},
                DemoOrderOutput,
                context,
            )

    order_tool = tool_catalog.get("commerce_get_order_status")
    if order_tool is not None and context.role in order_tool.allowed_roles:

        @server.tool(
            title="Commerce: order status",
            description=order_tool.description,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            meta={
                "resolveai/category": ToolCategory.COMMERCE.value,
                "resolveai/operation": ToolOperation.READ.value,
            },
        )
        async def commerce_get_order_status(
            order_number: OrderNumberArgument,
        ) -> OrderStatusToolOutput:
            """Look up an order and its shipment status for the active organization."""
            result = await _invoke(
                "commerce_get_order_status",
                {"order_number": order_number},
                OrderStatusToolOutput,
                context,
            )
            return result

    refund_tool = tool_catalog.get("commerce_get_refund_status")
    if refund_tool is not None and context.role in refund_tool.allowed_roles:

        @server.tool(
            title="Commerce: refund status",
            description=refund_tool.description,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            meta={
                "resolveai/category": ToolCategory.COMMERCE.value,
                "resolveai/operation": ToolOperation.READ.value,
            },
        )
        async def commerce_get_refund_status(
            order_number: OrderNumberArgument,
        ) -> RefundStatusToolOutput:
            """Read payment and refund statuses without performing a refund."""
            result = await _invoke(
                "commerce_get_refund_status",
                {"order_number": order_number},
                RefundStatusToolOutput,
                context,
            )
            return result

    ticket_tool = tool_catalog.get("support_create_ticket")
    if ticket_tool is not None and context.role in ticket_tool.allowed_roles:

        @server.tool(
            title="Support: create ticket",
            description=ticket_tool.description,
            annotations=ToolAnnotations(
                read_only_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
            meta={
                "resolveai/category": ToolCategory.SUPPORT.value,
                "resolveai/operation": ToolOperation.WRITE.value,
                "resolveai/required_roles": [
                    MembershipRole.ADMIN.value,
                    MembershipRole.SUPERVISOR.value,
                    MembershipRole.SUPPORT_AGENT.value,
                    MembershipRole.CUSTOMER.value,
                ],
            },
        )
        async def support_create_ticket(
            subject: Annotated[str, Field(min_length=3, max_length=180)],
            description: Annotated[str, Field(min_length=1, max_length=4000)],
            priority: SupportTicketPriority = SupportTicketPriority.NORMAL,
            order_number: OrderNumberArgument | None = None,
        ) -> CreateSupportTicketToolOutput:
            """Create an idempotent support ticket for the linked conversation customer."""
            result = await _invoke(
                "support_create_ticket",
                {
                    "subject": subject,
                    "description": description,
                    "priority": priority,
                    "order_number": order_number,
                },
                CreateSupportTicketToolOutput,
                context,
            )
            return result

    return server
