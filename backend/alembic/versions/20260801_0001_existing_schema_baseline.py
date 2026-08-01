"""Mark adoption of the existing SQL schema baseline.

Revision ID: 20260801_0001
Revises:
Create Date: 2026-08-01

The executable baseline DDL lives in database/schema.sql. Fresh databases load
that file and stamp this revision. Existing databases created from that same file
validate the baseline and stamp this revision before applying later migrations.
"""

from collections.abc import Sequence

revision: str = "20260801_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
