from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from app.main import create_app, lifespan


def test_application_factory_builds_fresh_mcp_session_manager() -> None:
    first = create_app()
    second = create_app()

    assert first.state.mcp_server is not second.state.mcp_server
    assert first.state.mcp_server.session_manager is not second.state.mcp_server.session_manager


async def test_lifespan_disposes_engine_after_application_error() -> None:
    dispose = AsyncMock()
    fake_engine = SimpleNamespace(dispose=dispose)

    @asynccontextmanager
    async def run_session_manager():
        yield

    fake_server = SimpleNamespace(
        session_manager=SimpleNamespace(run=run_session_manager),
    )
    test_app = FastAPI()
    test_app.state.mcp_server = fake_server

    with patch("app.main.engine", fake_engine):
        with pytest.raises(RuntimeError, match="application failed"):
            async with lifespan(test_app):
                raise RuntimeError("application failed")

    dispose.assert_awaited_once()
