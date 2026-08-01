from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.task import SocialPlatform


def _enum_values(enum: type[SocialPlatform]) -> list[str]:
    return [member.value for member in enum]


class SocialAccount(Base):
    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "platform",
            name="social_accounts_user_id_platform_key",
        ),
        UniqueConstraint(
            "platform",
            "profile_url",
            name="social_accounts_platform_profile_url_key",
        ),
        UniqueConstraint(
            "id",
            "user_id",
            "platform",
            name="social_accounts_id_user_id_platform_key",
        ),
        CheckConstraint(
            "profile_url ~* '^https://[^[:space:]]+$'",
            name="social_accounts_profile_url_check",
        ),
        CheckConstraint(
            "follower_count >= 0",
            name="social_accounts_follower_count_check",
        ),
        CheckConstraint(
            "following_count >= 0",
            name="social_accounts_following_count_check",
        ),
        CheckConstraint(
            "jsonb_typeof(enrichment_data) = 'object'",
            name="social_accounts_enrichment_data_check",
        ),
        CheckConstraint(
            "platform = 'reddit' OR (reddit_post_karma IS NULL AND reddit_comment_karma IS NULL)",
            name="social_accounts_platform_check",
        ),
        CheckConstraint(
            "NOT is_verified OR verified_at IS NOT NULL",
            name="social_accounts_is_verified_check",
        ),
        Index(
            "social_accounts_eligibility_idx",
            "platform",
            "is_verified",
            "follower_count",
            "karma",
        ),
        {
            "comment": "Freelancer-submitted public Reddit/LinkedIn account URL, "
            "provider validation, and latest influence metrics; no username or "
            "OAuth connection."
        },
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SocialPlatform] = mapped_column(
        ENUM(
            SocialPlatform,
            name="social_platform",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    profile_url: Mapped[str] = mapped_column(Text, nullable=False)
    follower_count: Mapped[int | None] = mapped_column(BigInteger)
    following_count: Mapped[int | None] = mapped_column(BigInteger)
    reddit_post_karma: Mapped[int | None] = mapped_column(BigInteger)
    reddit_comment_karma: Mapped[int | None] = mapped_column(BigInteger)
    karma: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN reddit_post_karma IS NULL AND reddit_comment_karma IS NULL "
            "THEN NULL ELSE COALESCE(reddit_post_karma, 0) + "
            "COALESCE(reddit_comment_karma, 0) END",
            persisted=True,
        ),
    )
    account_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrichment_provider: Mapped[str | None] = mapped_column(Text)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrichment_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    enrichment_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
