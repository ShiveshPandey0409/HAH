from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app import main as main_module
from app.main import create_app, lifespan


def test_application_factory_builds_fresh_mcp_session_manager() -> None:
    first = create_app()
    second = create_app()

    assert first.state.mcp_server is not second.state.mcp_server
    assert first.state.mcp_server.session_manager is not second.state.mcp_server.session_manager


def test_application_factory_allows_missing_webhook_runtime_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module.settings, "app_env", "development")
    monkeypatch.setattr(
        main_module,
        "runtime_from_settings",
        Mock(side_effect=RuntimeError("invalid webhook runtime")),
    )

    application = main_module.create_app()

    assert application.state.webhook_runtime is None


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_application_factory_rejects_invalid_webhook_runtime_in_deployments(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    monkeypatch.setattr(main_module.settings, "app_env", app_env)
    monkeypatch.setattr(
        main_module,
        "runtime_from_settings",
        Mock(side_effect=RuntimeError("invalid webhook runtime")),
    )

    with pytest.raises(RuntimeError, match="invalid webhook runtime"):
        main_module.create_app()


async def test_unknown_paths_return_404_without_mcp_authentication(
    client: AsyncClient,
) -> None:
    for path in ("/", "/favicon.ico", "/v1/nonexistent", "/mcp/nonexistent"):
        response = await client.get(path)
        assert response.status_code == 404


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


async def test_lifespan_runs_embedded_webhook_worker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispose = AsyncMock()
    fake_engine = SimpleNamespace(dispose=dispose)
    runtime = object()
    worker_started = asyncio.Event()

    @asynccontextmanager
    async def run_session_manager():
        yield

    async def run_worker(*, runtime: object, stop_event: asyncio.Event) -> None:
        worker_started.set()
        await stop_event.wait()

    fake_server = SimpleNamespace(
        session_manager=SimpleNamespace(run=run_session_manager),
    )
    test_app = FastAPI()
    test_app.state.mcp_server = fake_server
    test_app.state.webhook_runtime = runtime
    monkeypatch.setattr(main_module.settings, "webhook_worker_enabled", True)

    with (
        patch("app.main.engine", fake_engine),
        patch("app.main.run_webhook_worker", side_effect=run_worker) as worker,
    ):
        async with lifespan(test_app):
            await asyncio.wait_for(worker_started.wait(), timeout=1)

    worker.assert_called_once()
    assert worker.call_args.kwargs["runtime"] is runtime
    assert worker.call_args.kwargs["stop_event"].is_set()
    dispose.assert_awaited_once()
