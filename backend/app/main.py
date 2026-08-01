from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.db import Database


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = Database(get_settings().database_url)
    await database.open()
    app.state.database = database

    try:
        yield
    finally:
        await database.close()


app = FastAPI(title="HAH API", lifespan=lifespan)
app.include_router(api_router)
