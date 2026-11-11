from celery import Celery

from app.core.config import settings

AGENT_WORKFLOW_TASK_NAME = "resolveai.agent.process_workflow"
KNOWLEDGE_INGEST_TASK_NAME = "resolveai.knowledge.ingest_version"
KNOWLEDGE_DELETE_TASK_NAME = "resolveai.knowledge.delete_document"

celery_app = Celery(
    "resolveai",
    broker=settings.redis_url.get_secret_value(),
    backend=settings.celery_result_backend.get_secret_value(),
    include=["resolveai_worker.tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    result_serializer="json",
    task_serializer="json",
    task_track_started=True,
    worker_prefetch_multiplier=1,
)
