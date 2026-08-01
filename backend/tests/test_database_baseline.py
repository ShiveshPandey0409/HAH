from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import BASELINE_REVISION, TEST_DATABASE_URL


async def test_sql_baseline_is_stamped_for_alembic_adoption() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()

    assert revision == BASELINE_REVISION
