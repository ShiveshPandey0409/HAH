import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from hah.db import Database


class HealthyPool:
    def __init__(self) -> None:
        self.connection_mock = AsyncMock()

    @asynccontextmanager
    async def connection(self, *, timeout: float) -> AsyncIterator[AsyncMock]:
        assert timeout == 2
        yield self.connection_mock


class FailingPool:
    def connection(self, *, timeout: float) -> None:
        assert timeout == 2
        raise RuntimeError("database unavailable")


def test_database_is_ready() -> None:
    database = Database(None)
    pool = HealthyPool()
    database._pool = pool  # type: ignore[assignment]

    assert asyncio.run(database.is_ready()) is True
    pool.connection_mock.execute.assert_awaited_once_with("SELECT 1")


def test_database_is_not_ready_when_connection_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = Database(None)
    database._pool = FailingPool()  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR):
        is_ready = asyncio.run(database.is_ready())

    assert is_ready is False
    assert "Database readiness check failed" in caplog.text
