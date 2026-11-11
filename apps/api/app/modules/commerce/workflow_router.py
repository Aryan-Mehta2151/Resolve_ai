from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from app.core.auth import CurrentUser, SessionDep, require_roles
from app.core.config import settings
from app.db.models import BusinessAction, MembershipRole, Order
from app.modules.commerce.router import SupportAgent
from app.modules.commerce.workflows import BusinessRuleError, apply_action, decide_refund

router = APIRouter(prefix="/api/v1", tags=["demo business workflows"])
Reviewer = Annotated[
    CurrentUser,
    Depends(
        require_roles(
            MembershipRole.ADMIN,
            MembershipRole.SUPERVISOR,
        )
    ),
]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=240)]


class ActionCreate(BaseModel):
    kind: Literal["cancellation", "refund"]
    reason: Reason
    confirmed: bool


class DecisionCreate(BaseModel):
    approve: bool
    note: Reason


class ActionResponse(BaseModel):
    id: UUID
    order_id: UUID
    kind: str
    status: str
    reason: str
    requester_id: UUID
    reviewer_id: UUID | None
    decision_note: str | None
    created_at: datetime
    decided_at: datetime | None
    demo: bool = True
    model_config = ConfigDict(from_attributes=True)


def require_demo_workflows() -> None:
    if not settings.demo_business_workflows_enabled:
        raise HTTPException(503, "Demo business workflows are disabled")


@router.get("/business-actions", response_model=list[ActionResponse])
async def list_actions(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ActionResponse]:
    actions = await session.scalars(
        select(BusinessAction)
        .where(
            BusinessAction.organization_id == user.organization_id,
        )
        .order_by(BusinessAction.created_at.desc(), BusinessAction.id)
        .limit(limit)
        .offset(offset)
    )
    return [ActionResponse.model_validate(action) for action in actions]


@router.post("/orders/{order_id}/actions", response_model=ActionResponse)
async def create_action(
    order_id: UUID,
    body: ActionCreate,
    user: SupportAgent,
    session: SessionDep,
) -> ActionResponse:
    require_demo_workflows()
    if not body.confirmed:
        raise HTTPException(422, "Explicit confirmation is required")
    exists = await session.scalar(
        select(Order.id).where(
            Order.id == order_id,
            Order.organization_id == user.organization_id,
        )
    )
    if exists is None:
        raise HTTPException(404, "Order not found")
    try:
        action = await apply_action(
            session,
            user.organization_id,
            order_id,
            user.user_id,
            body.kind,
            body.reason,
        )
        await session.commit()
    except BusinessRuleError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
    return ActionResponse.model_validate(action)


@router.post("/business-actions/{action_id}/decision", response_model=ActionResponse)
async def review_action(
    action_id: UUID,
    body: DecisionCreate,
    user: Reviewer,
    session: SessionDep,
) -> ActionResponse:
    require_demo_workflows()
    exists = await session.scalar(
        select(BusinessAction.id).where(
            BusinessAction.id == action_id,
            BusinessAction.organization_id == user.organization_id,
        )
    )
    if exists is None:
        raise HTTPException(404, "Request not found")
    try:
        action = await decide_refund(
            session,
            user.organization_id,
            action_id,
            user.user_id,
            body.approve,
            body.note,
        )
        await session.commit()
    except BusinessRuleError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
    return ActionResponse.model_validate(action)
