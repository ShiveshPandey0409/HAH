"""PostgreSQL integration fixtures for the backend."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/hire_human_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(BACKEND_DIR / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


@pytest_asyncio.fixture(autouse=True)
async def clean_users(migrated_database: None) -> AsyncIterator[None]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    yield
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    await engine.dispose()


@pytest_asyncio.fixture
async def client(migrated_database: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
