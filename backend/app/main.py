from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import engine
from app.mcp.server import create_mcp_server

settings = get_settings()


async def safe_request_validation_error(
    _: Request,
    error: RequestValidationError,
) -> JSONResponse:
    safe_errors = [
        {key: value for key, value in item.items() if key not in {"input", "ctx"}}
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": safe_errors},
    )


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
    application.add_exception_handler(RequestValidationError, safe_request_validation_error)
    application.include_router(api_router)
    application.mount("/", mcp_http_app)
    return application


app = create_app()
