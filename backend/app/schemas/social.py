from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.task import SocialPlatform


class SocialProfilePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_url: str

    @field_validator("profile_url")
    @classmethod
    def reject_blank_profile_url(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("profile_url cannot be empty")
        return value


class SocialProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    platform: SocialPlatform
    profile_url: str
    follower_count: int | None
    following_count: int | None
    reddit_post_karma: int | None
    reddit_comment_karma: int | None
    karma: int | None
    account_created_at: datetime | None
    is_verified: bool
    verified_at: datetime | None
    enrichment_provider: str | None
    enriched_at: datetime | None
    created_at: datetime
    updated_at: datetime
