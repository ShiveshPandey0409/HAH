"""Link MCP task-creation requests to their tasks.

Revision ID: 20260802_0003
Revises: 20260802_0002
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0003"
down_revision: str | None = "20260802_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_requests", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "mcp_requests_task_id_fkey",
        "mcp_requests",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("mcp_requests_task_id_fkey", "mcp_requests", type_="foreignkey")
    op.drop_column("mcp_requests", "task_id")
