from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import marketplace, social_profiles, tasks, users

router = APIRouter()
router.include_router(users.router)
router.include_router(tasks.router)
router.include_router(social_profiles.router)
router.include_router(marketplace.router)
