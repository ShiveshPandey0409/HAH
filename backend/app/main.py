from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.db.session import engine, get_db_session

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(v1_router, prefix="/v1")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["system"])
async def readiness(
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready"},
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ready"},
    )
