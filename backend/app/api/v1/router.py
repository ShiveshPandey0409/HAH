from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import auth, marketplace, social_profiles, submissions, tasks, webhooks

router = APIRouter()
router.include_router(auth.router)
router.include_router(tasks.router)
router.include_router(social_profiles.router)
router.include_router(marketplace.router)
router.include_router(submissions.router)
router.include_router(webhooks.router)
