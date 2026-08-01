from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from app.main import lifespan


async def test_lifespan_disposes_engine_after_application_error() -> None:
    dispose = AsyncMock()
    fake_engine = SimpleNamespace(dispose=dispose)

    with patch("app.main.engine", fake_engine):
        with pytest.raises(RuntimeError, match="application failed"):
            async with lifespan(FastAPI()):
                raise RuntimeError("application failed")

    dispose.assert_awaited_once()
