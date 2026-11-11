from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import (
    AgentWorkflowStatus,
    ConversationMessageRole,
    ConversationStatus,
)


class AgentIntent(StrEnum):
    PLACE_ORDER = "place_order"
    LIST_PRODUCTS = "list_products"
    ORDER_STATUS = "order_status"
    SHIPMENT_TRACKING = "shipment_tracking"
    REFUND_STATUS = "refund_status"
    SUPPORT_TICKET = "support_ticket"
    CANCELLATION = "cancellation"
    HUMAN_ESCALATION = "human_escalation"
    GREETING = "greeting"
    GENERAL = "general"


class IntentAnalysis(BaseModel):
    intent: AgentIntent
    entities: dict[str, str] = Field(default_factory=dict)
    escalation_required: bool = False


class MessageCreate(BaseModel):
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class ConversationCreate(BaseModel):
    customer_id: UUID | None = None


class ConversationReview(BaseModel):
    status: Annotated[str, StringConstraints(pattern=r"^(open|resolved)$")]
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=240)]


class ConversationMessageResponse(BaseModel):
    id: UUID
    role: ConversationMessageRole
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConversationResponse(BaseModel):
    id: UUID
    customer_id: UUID | None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class WorkflowAcceptedResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    status: AgentWorkflowStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowEventResponse(BaseModel):
    id: UUID
    event_type: str
    payload: dict[str, object]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    status: AgentWorkflowStatus
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    events: list[WorkflowEventResponse] = Field(default_factory=list)
