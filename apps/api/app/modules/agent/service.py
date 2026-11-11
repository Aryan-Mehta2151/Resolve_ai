import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from mcp import Client
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import (
    AgentWorkflow,
    AgentWorkflowStatus,
    Conversation,
    ConversationMessage,
    ConversationMessageRole,
    ConversationStatus,
    MembershipRole,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    User,
    WorkflowEvent,
)
from app.modules.agent.graph import (
    MAX_CONTEXT_MESSAGES,
    MAX_GRAPH_STEPS,
    AgentState,
    build_agent_graph,
)
from app.modules.agent.provider import get_intent_classifier, get_response_generator
from app.modules.knowledge.service import create_knowledge_service
from app.modules.tools.catalog import ToolExecutionContext
from app.modules.tools.mcp_server import build_mcp_server

logger = logging.getLogger(__name__)


async def _record_event(
    session_factory: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    workflow_id: UUID,
    event_type: str,
    payload: dict[str, object],
) -> None:
    async with session_factory() as session, session.begin():
        latest_sequence = await session.scalar(
            select(func.max(WorkflowEvent.sequence_number)).where(
                WorkflowEvent.organization_id == organization_id,
                WorkflowEvent.workflow_id == workflow_id,
            )
        )
        session.add(
            WorkflowEvent(
                organization_id=organization_id,
                workflow_id=workflow_id,
                sequence_number=(latest_sequence or 0) + 1,
                event_type=event_type,
                payload=payload,
            )
        )


async def _claim_workflow(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_id: UUID,
) -> tuple[UUID, UUID, UUID, UUID] | None:
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        workflow = await session.scalar(
            select(AgentWorkflow).where(AgentWorkflow.id == workflow_id).with_for_update()
        )
        if workflow is None or workflow.status in {
            AgentWorkflowStatus.COMPLETED.value,
            AgentWorkflowStatus.FAILED.value,
        }:
            return None

        workflow.status = AgentWorkflowStatus.RUNNING.value
        workflow.started_at = now
        workflow.error_code = None
        return (
            workflow.organization_id,
            workflow.conversation_id,
            workflow.trigger_message_id,
            workflow.requested_by_user_id,
        )


def _advisory_lock_keys(conversation_id: UUID) -> tuple[int, int]:
    high = (conversation_id.int >> 96) & 0xFFFFFFFF
    low = (conversation_id.int >> 64) & 0xFFFFFFFF
    return tuple(value - 0x100000000 if value >= 0x80000000 else value for value in (high, low))


@asynccontextmanager
async def _workflow_execution_lock(
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: UUID,
) -> AsyncGenerator[None, None]:
    async with session_factory() as session:
        engine = session.bind
    if not isinstance(engine, AsyncEngine):
        raise RuntimeError("Agent workflow requires an asynchronous database engine")
    if engine.dialect.name == "sqlite":
        yield
        return
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Agent workflow locking is not configured for this database")

    lock_keys = _advisory_lock_keys(conversation_id)
    async with engine.connect() as connection:
        await connection.scalar(select(func.pg_advisory_lock(*lock_keys)))
        await connection.commit()
        try:
            yield
        finally:
            await connection.scalar(select(func.pg_advisory_unlock(*lock_keys)))
            await connection.commit()


async def _execute_claimed_workflow(
    workflow_id: UUID,
    organization_id: UUID,
    conversation_id: UUID,
    trigger_message_id: UUID,
    requested_by_user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    checkpointer: BaseCheckpointSaver,
) -> None:
    knowledge_service = create_knowledge_service()
    try:
        async with session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == organization_id,
                )
            )
            if conversation is None:
                raise RuntimeError("Workflow conversation is unavailable")
            membership = await session.scalar(
                select(OrganizationMembership)
                .join(User, User.id == OrganizationMembership.user_id)
                .join(Organization, Organization.id == OrganizationMembership.organization_id)
                .where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == requested_by_user_id,
                    OrganizationMembership.status == MembershipStatus.ACTIVE.value,
                    User.is_active.is_(True),
                    Organization.is_active.is_(True),
                )
            )
            if membership is None:
                raise RuntimeError("Workflow requester is no longer an active organization member")
            try:
                actor_role = MembershipRole(membership.role)
            except ValueError as exc:
                raise RuntimeError("Workflow requester has an invalid organization role") from exc
            if actor_role == MembershipRole.CUSTOMER and (
                membership.customer_id is None
                or membership.customer_id != conversation.customer_id
            ):
                raise RuntimeError("Customer workflow requester does not own the conversation")
            trigger_message = await session.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.id == trigger_message_id,
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.organization_id == organization_id,
                )
            )
            if (
                trigger_message is None
                or trigger_message.role != ConversationMessageRole.USER.value
            ):
                raise RuntimeError("Workflow trigger message is unavailable")
            messages = await session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.organization_id == organization_id,
                    ConversationMessage.sequence_number <= trigger_message.sequence_number,
                )
                .order_by(ConversationMessage.sequence_number.desc())
                .limit(MAX_CONTEXT_MESSAGES)
            )
            context = [
                {"role": message.role, "content": message.content}
                for message in reversed(list(messages))
            ]

        async def record_event(event_type: str, payload: dict[str, object]) -> None:
            await _record_event(
                session_factory,
                organization_id,
                workflow_id,
                event_type,
                payload,
            )

        await record_event("workflow.started", {"conversation_id": str(conversation_id)})
        tool_context = ToolExecutionContext(
            organization_id=organization_id,
            actor_user_id=requested_by_user_id,
            role=actor_role,
            workflow_id=workflow_id,
            conversation_id=conversation_id,
            customer_id=conversation.customer_id,
            session_factory=session_factory,
        )
        tool_server = build_mcp_server(tool_context)

        async def answer_knowledge(question: str):
            async with session_factory() as knowledge_session:
                return await knowledge_service.answer(
                    knowledge_session,
                    organization_id,
                    question,
                )

        initial_state: AgentState = {
            "conversation_id": str(conversation_id),
            "organization_id": str(organization_id),
            "workflow_id": str(workflow_id),
            "customer_id": (
                str(conversation.customer_id) if conversation.customer_id is not None else None
            ),
            "call_id": None,
            "messages": context,
            "latest_message": trigger_message.content,
        }
        async with Client(tool_server) as tool_client:
            available_tools = await tool_client.list_tools()
            available_tool_names = frozenset(tool.name for tool in available_tools.tools)
            graph = build_agent_graph(
                checkpointer,
                record_event,
                get_intent_classifier(),
                tool_client,
                available_tool_names,
                answer_knowledge if knowledge_service.provider is not None else None,
                response_generator=get_response_generator(),
            )
            result = await graph.ainvoke(
                initial_state,
                config={
                    "configurable": {"thread_id": str(conversation_id)},
                    "recursion_limit": MAX_GRAPH_STEPS,
                },
            )
        response = result.get("response")
        if not isinstance(response, str) or not response:
            raise RuntimeError("Agent graph completed without a response")

        async with session_factory() as session, session.begin():
            workflow = await session.scalar(
                select(AgentWorkflow).where(
                    AgentWorkflow.id == workflow_id,
                    AgentWorkflow.organization_id == organization_id,
                )
            )
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == organization_id,
                )
            )
            if workflow is None or conversation is None:
                raise RuntimeError("Workflow records are unavailable")
            latest_sequence = await session.scalar(
                select(func.max(ConversationMessage.sequence_number)).where(
                    ConversationMessage.organization_id == organization_id,
                    ConversationMessage.conversation_id == conversation_id,
                )
            )
            session.add(
                ConversationMessage(
                    organization_id=organization_id,
                    conversation_id=conversation_id,
                    sequence_number=(latest_sequence or 0) + 1,
                    role=ConversationMessageRole.ASSISTANT.value,
                    content=response,
                )
            )
            workflow.status = AgentWorkflowStatus.COMPLETED.value
            workflow.completed_at = datetime.now(UTC)
            conversation.updated_at = datetime.now(UTC)
            if result.get("escalation_required"):
                conversation.status = ConversationStatus.ESCALATED.value
            latest_sequence = await session.scalar(
                select(func.max(WorkflowEvent.sequence_number)).where(
                    WorkflowEvent.organization_id == organization_id,
                    WorkflowEvent.workflow_id == workflow_id,
                )
            )
            session.add(
                WorkflowEvent(
                    organization_id=organization_id,
                    workflow_id=workflow_id,
                    sequence_number=(latest_sequence or 0) + 1,
                    event_type="workflow.completed",
                    payload={
                        "intent": result.get("intent", "general"),
                        "escalation_required": bool(result.get("escalation_required", False)),
                    },
                )
            )
    except Exception as exc:
        logger.error(
            "Agent workflow execution failed",
            extra={"workflow_id": str(workflow_id), "error_type": type(exc).__name__},
        )
        async with session_factory() as session, session.begin():
            workflow = await session.scalar(
                select(AgentWorkflow).where(AgentWorkflow.id == workflow_id)
            )
            if workflow is not None:
                workflow.status = AgentWorkflowStatus.FAILED.value
                workflow.error_code = "workflow_execution_failed"
                workflow.completed_at = datetime.now(UTC)
                latest_sequence = await session.scalar(
                    select(func.max(WorkflowEvent.sequence_number)).where(
                        WorkflowEvent.organization_id == workflow.organization_id,
                        WorkflowEvent.workflow_id == workflow.id,
                    )
                )
                session.add(
                    WorkflowEvent(
                        organization_id=workflow.organization_id,
                        workflow_id=workflow.id,
                        sequence_number=(latest_sequence or 0) + 1,
                        event_type="workflow.failed",
                        payload={"error_code": "workflow_execution_failed"},
                    )
                )
        raise
    finally:
        await knowledge_service.aclose()


async def run_agent_workflow(
    workflow_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    checkpointer: BaseCheckpointSaver,
) -> None:
    async with session_factory() as session:
        conversation_id = await session.scalar(
            select(AgentWorkflow.conversation_id).where(AgentWorkflow.id == workflow_id)
        )
    if conversation_id is None:
        return
    async with _workflow_execution_lock(session_factory, conversation_id):
        claimed = await _claim_workflow(session_factory, workflow_id)
        if claimed is None:
            return
        organization_id, conversation_id, trigger_message_id, requested_by_user_id = claimed
        await _execute_claimed_workflow(
            workflow_id,
            organization_id,
            conversation_id,
            trigger_message_id,
            requested_by_user_id,
            session_factory,
            checkpointer,
        )
