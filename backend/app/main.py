from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import engine
from app.mcp.server import create_mcp_server

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        async with app.state.mcp_server.session_manager.run():
            yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    mcp_server, mcp_http_app = create_mcp_server()
    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    application.state.mcp_server = mcp_server
    application.include_router(api_router)
    application.mount("/", mcp_http_app)
    return application


app = create_app()
