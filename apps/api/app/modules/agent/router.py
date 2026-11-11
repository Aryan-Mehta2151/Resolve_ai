from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.auth import CurrentUser, SessionDep
from app.db.models import (
    AgentWorkflow,
    AgentWorkflowStatus,
    Conversation,
    ConversationMessage,
    ConversationMessageRole,
    ConversationStatus,
    Customer,
    WorkflowEvent,
)
from app.modules.agent.dispatch import dispatch_workflow
from app.modules.agent.schemas import (
    ConversationCreate,
    ConversationMessageResponse,
    ConversationResponse,
    ConversationReview,
    MessageCreate,
    WorkflowAcceptedResponse,
    WorkflowEventResponse,
    WorkflowResponse,
)
from app.modules.commerce.router import SupportAgent

router = APIRouter(prefix="/api/v1", tags=["agent workflows"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    user: CurrentUser,
    session: SessionDep,
    status_filter: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[ConversationResponse]:
    statement = select(Conversation).where(Conversation.organization_id == user.organization_id)
    if status_filter is not None:
        statement = statement.where(Conversation.status == status_filter.value)
    conversations = await session.scalars(
        statement.order_by(Conversation.updated_at.desc(), Conversation.id)
        .limit(limit)
        .offset(offset)
    )
    return [ConversationResponse.model_validate(c) for c in conversations]


@router.post("/conversations/{conversation_id}/review", response_model=ConversationResponse)
async def review_conversation(
    conversation_id: UUID,
    body: ConversationReview,
    user: SupportAgent,
    session: SessionDep,
) -> ConversationResponse:
    conversation = await session.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.organization_id == user.organization_id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    if conversation.status != "escalated":
        raise HTTPException(409, "Only escalated conversations can be reviewed")
    workflow = await session.scalar(
        select(AgentWorkflow)
        .where(
            AgentWorkflow.conversation_id == conversation_id,
            AgentWorkflow.organization_id == user.organization_id,
        )
        .order_by(AgentWorkflow.created_at.desc(), AgentWorkflow.id)
        .limit(1)
        .with_for_update()
    )
    if workflow is None or workflow.status not in {"completed", "failed"}:
        raise HTTPException(409, "Wait until the active workflow finishes")
    sequence = await session.scalar(
        select(func.max(WorkflowEvent.sequence_number)).where(
            WorkflowEvent.workflow_id == workflow.id,
            WorkflowEvent.organization_id == user.organization_id,
        )
    )
    session.add(
        WorkflowEvent(
            organization_id=user.organization_id,
            workflow_id=workflow.id,
            sequence_number=(sequence or 0) + 1,
            event_type="conversation.reviewed",
            payload={"actor_id": str(user.user_id), "status": body.status, "note": body.note},
        )
    )
    conversation.status = body.status
    await session.commit()
    await session.refresh(conversation)
    return ConversationResponse.model_validate(conversation)


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: ConversationCreate,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationResponse:
    if payload.customer_id is not None:
        customer_exists = await session.scalar(
            select(Customer.id).where(
                Customer.id == payload.customer_id,
                Customer.organization_id == user.organization_id,
            )
        )
        if customer_exists is None:
            raise HTTPException(status_code=404, detail="Customer not found")

    conversation = Conversation(
        id=uuid4(),
        organization_id=user.organization_id,
        customer_id=payload.customer_id,
        created_by_user_id=user.user_id,
        status=ConversationStatus.OPEN.value,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return ConversationResponse.model_validate(conversation)


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 100,
    offset: Offset = 0,
) -> ConversationResponse:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.organization_id == user.organization_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await session.scalars(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.organization_id == user.organization_id,
        )
        .order_by(ConversationMessage.sequence_number)
        .limit(limit)
        .offset(offset)
    )
    return ConversationResponse(
        id=conversation.id,
        customer_id=conversation.customer_id,
        status=conversation.status,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[ConversationMessageResponse.model_validate(message) for message in messages],
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=WorkflowAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: CurrentUser,
    session: SessionDep,
) -> WorkflowAcceptedResponse:
    conversation = await session.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.organization_id == user.organization_id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.status != ConversationStatus.OPEN.value:
        raise HTTPException(status_code=409, detail="Conversation is not accepting agent messages")
    active_workflow = await session.scalar(
        select(AgentWorkflow.id).where(
            AgentWorkflow.conversation_id == conversation.id,
            AgentWorkflow.organization_id == user.organization_id,
            AgentWorkflow.status.in_(
                [AgentWorkflowStatus.QUEUED.value, AgentWorkflowStatus.RUNNING.value]
            ),
        )
    )
    if active_workflow is not None:
        raise HTTPException(
            status_code=409,
            detail="Conversation already has a workflow in progress",
        )
    latest_sequence = await session.scalar(
        select(func.max(ConversationMessage.sequence_number)).where(
            ConversationMessage.organization_id == user.organization_id,
            ConversationMessage.conversation_id == conversation.id,
        )
    )

    message_id = uuid4()
    session.add(
        ConversationMessage(
            id=message_id,
            organization_id=user.organization_id,
            conversation_id=conversation.id,
            sequence_number=(latest_sequence or 0) + 1,
            role=ConversationMessageRole.USER.value,
            content=payload.content,
        )
    )
    await session.flush()
    workflow = AgentWorkflow(
        id=uuid4(),
        organization_id=user.organization_id,
        conversation_id=conversation.id,
        requested_by_user_id=user.user_id,
        trigger_message_id=message_id,
        status=AgentWorkflowStatus.QUEUED.value,
    )
    session.add(workflow)
    session.add(
        WorkflowEvent(
            organization_id=user.organization_id,
            workflow_id=workflow.id,
            sequence_number=1,
            event_type="workflow.queued",
            payload={"conversation_id": str(conversation.id)},
        )
    )
    await session.commit()
    await session.refresh(workflow)

    await dispatch_workflow(session, workflow)

    return WorkflowAcceptedResponse.model_validate(workflow)


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: UUID,
    user: CurrentUser,
    session: SessionDep,
) -> WorkflowResponse:
    workflow = await session.scalar(
        select(AgentWorkflow).where(
            AgentWorkflow.id == workflow_id,
            AgentWorkflow.organization_id == user.organization_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    events = await session.scalars(
        select(WorkflowEvent)
        .where(
            WorkflowEvent.workflow_id == workflow.id,
            WorkflowEvent.organization_id == user.organization_id,
        )
        .order_by(WorkflowEvent.sequence_number)
    )
    return WorkflowResponse(
        id=workflow.id,
        conversation_id=workflow.conversation_id,
        status=workflow.status,
        error_code=workflow.error_code,
        started_at=workflow.started_at,
        completed_at=workflow.completed_at,
        created_at=workflow.created_at,
        events=[WorkflowEventResponse.model_validate(event) for event in events],
    )


@router.get("/workflows/{workflow_id}/events", response_model=list[WorkflowEventResponse])
async def list_workflow_events(
    workflow_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[WorkflowEventResponse]:
    workflow_exists = await session.scalar(
        select(AgentWorkflow.id).where(
            AgentWorkflow.id == workflow_id,
            AgentWorkflow.organization_id == user.organization_id,
        )
    )
    if workflow_exists is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    events = await session.scalars(
        select(WorkflowEvent)
        .where(
            WorkflowEvent.workflow_id == workflow_id,
            WorkflowEvent.organization_id == user.organization_id,
        )
        .order_by(WorkflowEvent.sequence_number)
        .limit(limit)
        .offset(offset)
    )
    return [WorkflowEventResponse.model_validate(event) for event in events]
