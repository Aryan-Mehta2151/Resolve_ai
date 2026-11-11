from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app import main


def test_live_health_does_not_require_dependencies() -> None:
    with TestClient(main.app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_health_reports_all_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "check_dependencies",
        AsyncMock(
            return_value={
                "database": "ok",
                "redis": "ok",
                "object_storage": "ok",
            }
        ),
    )

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "dependencies": {
            "database": "ok",
            "redis": "ok",
            "object_storage": "ok",
        },
    }


def test_ready_health_returns_503_when_a_dependency_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "check_dependencies",
        AsyncMock(
            return_value={
                "database": "ok",
                "redis": "unavailable",
                "object_storage": "ok",
            }
        ),
    )

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["dependencies"]["redis"] == "unavailable"
