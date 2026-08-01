"""Create the backend users table.

Revision ID: 20260801_0001
Revises:
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260801_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("can_create_tasks", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("can_work_tasks", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("prava_account_ref", sa.Text(), nullable=True),
        sa.Column("prava_account_status", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(display_name) <> ''", name="users_display_name_not_blank"),
        sa.CheckConstraint(
            "can_create_tasks OR can_work_tasks",
            name="users_has_capability",
        ),
        sa.CheckConstraint(
            "prava_account_status IS NULL OR "
            "prava_account_status IN ('pending', 'active', 'disabled')",
            name="users_prava_account_status_valid",
        ),
        sa.CheckConstraint(
            "(prava_account_ref IS NULL) = (prava_account_status IS NULL)",
            name="users_prava_account_pair",
        ),
        sa.PrimaryKeyConstraint("id", name="users_pkey"),
        sa.UniqueConstraint("email", name="users_email_key"),
        sa.UniqueConstraint("prava_account_ref", name="users_prava_account_ref_key"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          NEW.updated_at := now();
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER users_updated_at
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_updated_at ON users")
    op.drop_table("users")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
