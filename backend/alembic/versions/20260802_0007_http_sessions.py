"""Add password login identities and revocable HTTP sessions.

Revision ID: 20260802_0007
Revises: 20260802_0006
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0007"
down_revision: str | None = "20260802_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        "users_password_hash_check",
        "users",
        "password_hash IS NULL OR ("
        "password_hash = btrim(password_hash) "
        "AND password_hash LIKE 'scrypt$16384$8$1$%' "
        "AND char_length(password_hash) BETWEEN 80 AND 255)",
    )
    op.create_table(
        "user_sessions",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="user_sessions_token_hash_check",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="user_sessions_expiry_check",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="user_sessions_revoked_at_check",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="user_sessions_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="user_sessions_token_hash_key"),
        comment="Revocable HTTP login session; only a SHA-256 token hash is stored.",
    )
    op.create_index("user_sessions_user_id_idx", "user_sessions", ["user_id"])
    op.create_index("user_sessions_expires_at_idx", "user_sessions", ["expires_at"])
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="password_reset_tokens_token_hash_check",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="password_reset_tokens_expiry_check",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="password_reset_tokens_consumed_at_check",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="password_reset_tokens_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="password_reset_tokens_token_hash_key"),
        comment="Single-use password reset capability; only a SHA-256 hash is stored.",
    )
    op.create_index(
        "password_reset_tokens_user_id_idx",
        "password_reset_tokens",
        ["user_id"],
    )
    op.create_index(
        "password_reset_tokens_expires_at_idx",
        "password_reset_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM user_sessions)
                 OR EXISTS (SELECT 1 FROM password_reset_tokens)
                 OR EXISTS (SELECT 1 FROM users WHERE password_hash IS NOT NULL) THEN
                RAISE EXCEPTION 'refusing to discard HTTP authentication data'
                  USING ERRCODE = 'HMG02';
              END IF;
            END
            $$
            """
        )
    )
    op.drop_index(
        "password_reset_tokens_expires_at_idx",
        table_name="password_reset_tokens",
    )
    op.drop_index(
        "password_reset_tokens_user_id_idx",
        table_name="password_reset_tokens",
    )
    op.drop_table("password_reset_tokens")
    op.drop_index("user_sessions_expires_at_idx", table_name="user_sessions")
    op.drop_index("user_sessions_user_id_idx", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_constraint("users_password_hash_check", "users", type_="check")
    op.drop_column("users", "password_hash")
