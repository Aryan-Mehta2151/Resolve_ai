import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import AGENT_WORKFLOW_TASK_NAME, celery_app
from app.db.models import AgentWorkflow, WorkflowEvent

logger = logging.getLogger(__name__)


async def dispatch_workflow(session: AsyncSession, workflow: AgentWorkflow) -> None:
    try:
        celery_app.send_task(AGENT_WORKFLOW_TASK_NAME, args=[str(workflow.id)])
    except Exception as exc:
        logger.error(
            "Unable to enqueue agent workflow",
            extra={"workflow_id": str(workflow.id), "error_type": type(exc).__name__},
        )
        workflow.status = "failed"
        workflow.error_code = "workflow_dispatch_failed"
        session.add(
            WorkflowEvent(
                organization_id=workflow.organization_id,
                workflow_id=workflow.id,
                sequence_number=2,
                event_type="workflow.failed",
                payload={"error_code": "workflow_dispatch_failed"},
            )
        )
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Agent workflow could not be queued",
                "workflow_id": str(workflow.id),
            },
        ) from exc
