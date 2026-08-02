from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("btrim(display_name) <> ''", name="users_display_name_not_blank"),
        CheckConstraint("can_create_tasks OR can_work_tasks", name="users_has_capability"),
        CheckConstraint(
            "prava_account_status IS NULL OR "
            "prava_account_status IN ('pending', 'active', 'disabled')",
            name="users_prava_account_status_valid",
        ),
        CheckConstraint(
            "(prava_account_ref IS NULL) = (prava_account_status IS NULL)",
            name="users_prava_account_pair",
        ),
        CheckConstraint(
            "password_hash IS NULL OR ("
            "password_hash = btrim(password_hash) "
            "AND password_hash LIKE 'scrypt$16384$8$1$%' "
            "AND char_length(password_hash) BETWEEN 80 AND 255)",
            name="users_password_hash_check",
        ),
        {
            "comment": "Creator and/or freelancer account, including 1:1 profile "
            "and Prava account reference."
        },
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    can_create_tasks: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    can_work_tasks: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    bio: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    prava_account_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    prava_account_status: Mapped[str | None] = mapped_column(Text)
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


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="user_sessions_token_hash_check",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="user_sessions_expiry_check",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="user_sessions_revoked_at_check",
        ),
        Index("user_sessions_user_id_idx", "user_id"),
        Index("user_sessions_expires_at_idx", "expires_at"),
        {"comment": "Revocable HTTP login session; only a SHA-256 token hash is stored."},
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
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="password_reset_tokens_token_hash_check",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="password_reset_tokens_expiry_check",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="password_reset_tokens_consumed_at_check",
        ),
        Index("password_reset_tokens_user_id_idx", "user_id"),
        Index("password_reset_tokens_expires_at_idx", "expires_at"),
        {"comment": "Single-use password reset capability; only a SHA-256 hash is stored."},
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
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
