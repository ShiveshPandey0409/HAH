from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import health as health_routes
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


async def test_readiness_times_out(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_session = AsyncMock(spec=AsyncSession)

    async def block_forever(_: object) -> None:
        await asyncio.Event().wait()

    blocked_session.execute.side_effect = block_forever

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield blocked_session

    monkeypatch.setattr(health_routes, "READINESS_TIMEOUT_SECONDS", 0.01)
    app.dependency_overrides[get_db_session] = override_session
    try:
        response = await client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
