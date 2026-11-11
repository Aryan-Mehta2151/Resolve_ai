import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.modules.agent.router import router as agent_router
from app.modules.auth.router import router as auth_router
from app.modules.commerce.router import router as commerce_router
from app.modules.commerce.workflow_router import router as business_router
from app.modules.customer.router import router as customer_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.knowledge.service import create_knowledge_service
from app.modules.knowledge.storage import KnowledgeObjectStorage, KnowledgeStorageError
from app.modules.voice.router import router as voice_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"timeout": settings.healthcheck_timeout_seconds},
    )
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=settings.healthcheck_timeout_seconds,
        socket_timeout=settings.healthcheck_timeout_seconds,
    )
    knowledge_service = create_knowledge_service()
    application.state.database_engine = engine
    application.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    application.state.redis = redis
    application.state.knowledge_service = knowledge_service
    try:
        yield
    finally:
        await knowledge_service.aclose()
        await redis.aclose()
        await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(commerce_router)
app.include_router(business_router)
app.include_router(customer_router)
app.include_router(agent_router)
app.include_router(knowledge_router)
app.include_router(voice_router)


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def _check_database(engine: AsyncEngine) -> str:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("Database readiness check failed", extra={"error_type": type(exc).__name__})
        return "unavailable"
    return "ok"


async def _check_redis(redis: Redis) -> str:
    try:
        await redis.ping()
    except RedisError as exc:
        logger.warning("Redis readiness check failed", extra={"error_type": type(exc).__name__})
        return "unavailable"
    return "ok"


async def _check_object_storage(storage: KnowledgeObjectStorage) -> str:
    try:
        await storage.check_health()
    except KnowledgeStorageError as exc:
        logger.warning(
            "Object storage readiness check failed",
            extra={"error_type": type(exc).__name__},
        )
        return "unavailable"
    return "ok"


async def check_dependencies(request: Request) -> dict[str, str]:
    return {
        "database": await _check_database(request.app.state.database_engine),
        "redis": await _check_redis(request.app.state.redis),
        "object_storage": await _check_object_storage(
            request.app.state.knowledge_service.storage
        ),
    }


@app.get("/health/ready", tags=["health"])
async def ready(request: Request) -> dict[str, object]:
    dependencies = await check_dependencies(request)
    if any(status != "ok" for status in dependencies.values()):
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependencies": dependencies},
        )
    return {"status": "ok", "dependencies": dependencies}
