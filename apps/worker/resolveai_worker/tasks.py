import asyncio
import platform
from uuid import UUID

from app.core.celery import (
    AGENT_WORKFLOW_TASK_NAME,
    KNOWLEDGE_DELETE_TASK_NAME,
    KNOWLEDGE_INGEST_TASK_NAME,
)
from app.core.config import settings
from app.modules.agent.service import run_agent_workflow
from app.modules.knowledge.provider import KnowledgeProviderError
from app.modules.knowledge.service import create_knowledge_service
from app.modules.knowledge.storage import KnowledgeStorageError
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resolveai_worker.celery_app import celery_app

_CHECKPOINT_SETUP_LOCK = (0x524149, 0x4C4752)


@celery_app.task(name="resolveai.healthcheck")
def healthcheck() -> str:
    return "ok"


async def _run_agent_workflow(workflow_id: UUID) -> None:
    database_url = settings.database_url.get_secret_value()
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    checkpoint_url = (
        make_url(database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    try:
        async with AsyncPostgresSaver.from_conn_string(
            checkpoint_url,
            serde=JsonPlusSerializer(allowed_msgpack_modules=None),
        ) as checkpointer:
            async with await AsyncConnection.connect(
                checkpoint_url,
                autocommit=True,
                row_factory=dict_row,
            ) as setup_connection:
                await setup_connection.execute(
                    "SELECT pg_advisory_lock(%s, %s)",
                    _CHECKPOINT_SETUP_LOCK,
                )
                try:
                    await checkpointer.setup()
                finally:
                    await setup_connection.execute(
                        "SELECT pg_advisory_unlock(%s, %s)",
                        _CHECKPOINT_SETUP_LOCK,
                    )
            await run_agent_workflow(workflow_id, session_factory, checkpointer)
    finally:
        await engine.dispose()


@celery_app.task(
    name=AGENT_WORKFLOW_TASK_NAME,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_agent_workflow(workflow_id: str) -> None:
    workflow = _run_agent_workflow(UUID(workflow_id))
    if platform.system() == "Windows":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(workflow)
    else:
        asyncio.run(workflow)


async def _run_knowledge_ingestion(
    organization_id: UUID,
    document_id: UUID,
    version_id: UUID,
) -> None:
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    knowledge_service = create_knowledge_service()
    try:
        await knowledge_service.ingest_version(
            session_factory,
            organization_id,
            document_id,
            version_id,
        )
    finally:
        await knowledge_service.aclose()
        await engine.dispose()


@celery_app.task(
    name=KNOWLEDGE_INGEST_TASK_NAME,
    acks_late=True,
    autoretry_for=(KnowledgeProviderError, KnowledgeStorageError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def ingest_knowledge_version(
    organization_id: str, document_id: str, version_id: str
) -> None:
    ingestion = _run_knowledge_ingestion(
        UUID(organization_id),
        UUID(document_id),
        UUID(version_id),
    )
    if platform.system() == "Windows":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(ingestion)
    else:
        asyncio.run(ingestion)


async def _run_knowledge_deletion(organization_id: UUID, document_id: UUID) -> None:
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    knowledge_service = create_knowledge_service()
    try:
        await knowledge_service.delete_document(
            session_factory, organization_id, document_id
        )
    finally:
        await knowledge_service.aclose()
        await engine.dispose()


@celery_app.task(
    name=KNOWLEDGE_DELETE_TASK_NAME,
    acks_late=True,
    autoretry_for=(KnowledgeProviderError, KnowledgeStorageError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def delete_knowledge_document(organization_id: str, document_id: str) -> None:
    deletion = _run_knowledge_deletion(UUID(organization_id), UUID(document_id))
    if platform.system() == "Windows":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(deletion)
    else:
        asyncio.run(deletion)
