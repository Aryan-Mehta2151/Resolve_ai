import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import main
from app.core.config import settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_session


@pytest.fixture
def database_session_factory(tmp_path):
    database_path = tmp_path / "test.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")

    @event.listens_for(sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        poolclass=NullPool,
    )

    @event.listens_for(async_engine.sync_engine, "connect")
    def enable_async_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield async_sessionmaker(async_engine, expire_on_commit=False)
    asyncio.run(async_engine.dispose())


@pytest.fixture
def client(database_session_factory, monkeypatch) -> Iterator[TestClient]:
    async def override_get_session():
        async with database_session_factory() as session:
            yield session

    monkeypatch.setattr(
        settings, "jwt_secret_key", SecretStr("test-only-secret-value-that-is-at-least-32")
    )
    monkeypatch.setattr(settings, "allow_self_registration", True)
    monkeypatch.setattr(settings, "vapi_reachability_check_enabled", False)
    main.app.dependency_overrides[get_session] = override_get_session
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()
