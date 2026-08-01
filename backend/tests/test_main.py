import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from hah.main import lifespan


def test_lifespan_closes_database_after_error() -> None:
    database = AsyncMock()

    async def run_lifespan() -> None:
        with patch("hah.main.Database", return_value=database):
            with pytest.raises(RuntimeError, match="application failed"):
                async with lifespan(FastAPI()):
                    raise RuntimeError("application failed")

    asyncio.run(run_lifespan())

    database.open.assert_awaited_once()
    database.close.assert_awaited_once()
