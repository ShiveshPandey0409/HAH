"""PostgreSQL integration fixtures for the backend."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.db.alembic_config import escape_alembic_config_value
from tests.database_safety import require_safe_test_database_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BACKEND_DIR.parent / "database" / "schema.sql"
BASELINE_REVISION = "20260801_0001"

TEST_DATABASE_URL = require_safe_test_database_url(
    os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5433/hire_human_test",
    )
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault(
    "WEBHOOK_SECRET_ENCRYPTION_KEYS",
    '["MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="]',
)

from app.main import app  # noqa: E402


async def reset_to_sql_baseline() -> None:
    parsed_url = make_url(TEST_DATABASE_URL).set(drivername="postgresql")
    connection = await asyncpg.connect(parsed_url.render_as_string(hide_password=False))
    try:
        await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await connection.execute(SCHEMA_PATH.read_text())
    finally:
        await connection.close()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    asyncio.run(reset_to_sql_baseline())

    config = Config(BACKEND_DIR / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        escape_alembic_config_value(TEST_DATABASE_URL),
    )
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")
    yield


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
