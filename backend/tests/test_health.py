from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.main import app


async def test_health_checks_process(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_checks_database(client: AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readiness_reports_database_failure(client: AsyncClient) -> None:
    unavailable_session = AsyncMock(spec=AsyncSession)
    unavailable_session.execute.side_effect = OSError("database unavailable")

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield unavailable_session

    app.dependency_overrides[get_db_session] = override_session
    try:
        response = await client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
