from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import tasks, users

router = APIRouter()
router.include_router(users.router)
router.include_router(tasks.router)
