"""Track Prava allowance pools funded in one provider charge.

Revision ID: 20260802_0012
Revises: 20260802_0011
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0012"
down_revision: str | None = "20260802_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_authorizations",
        sa.Column(
            "pool_funded_once",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("payment_authorizations", "pool_funded_once")
