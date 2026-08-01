from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from hah.db import Database
from hah.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_without_database() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_with_available_database() -> None:
    with patch.object(Database, "is_ready", new=AsyncMock(return_value=True)):
        with TestClient(app) as client:
            response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
