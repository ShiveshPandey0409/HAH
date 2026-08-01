"""Add task and bounty cross-row guards.

Revision ID: 20260802_0002
Revises: 20260801_0001
Create Date: 2026-08-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260802_0002"
down_revision: str | None = "20260801_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION hah_jsonb_text_array_is_unique(value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $$
          SELECT jsonb_array_length(value) = count(DISTINCT item)
            FROM jsonb_array_elements_text(value) AS items(item)
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE bounties
          ADD CONSTRAINT bounties_proof_requirements_unique
          CHECK (hah_jsonb_text_array_is_unique(proof_requirements))
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_bounty_task_deadline()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_task_deadline timestamptz;
        BEGIN
          SELECT deadline_at INTO v_task_deadline FROM tasks WHERE id = NEW.task_id;
          IF NEW.deadline_at IS NOT NULL
             AND v_task_deadline IS NOT NULL
             AND NEW.deadline_at > v_task_deadline THEN
            RAISE EXCEPTION 'bounty deadline cannot be after the task deadline';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER bounties_enforce_task_deadline
        BEFORE INSERT OR UPDATE OF task_id, deadline_at ON bounties
        FOR EACH ROW EXECUTE FUNCTION enforce_bounty_task_deadline()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_task_bounty_deadlines()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.deadline_at IS NOT NULL AND EXISTS (
            SELECT 1 FROM bounties
             WHERE task_id = NEW.id
               AND deadline_at IS NOT NULL
               AND deadline_at > NEW.deadline_at
          ) THEN
            RAISE EXCEPTION 'task deadline cannot be before a bounty deadline';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tasks_enforce_bounty_deadlines
        BEFORE UPDATE OF deadline_at ON tasks
        FOR EACH ROW EXECUTE FUNCTION enforce_task_bounty_deadlines()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tasks_enforce_bounty_deadlines ON tasks")
    op.execute("DROP FUNCTION IF EXISTS enforce_task_bounty_deadlines()")
    op.execute("DROP TRIGGER IF EXISTS bounties_enforce_task_deadline ON bounties")
    op.execute("DROP FUNCTION IF EXISTS enforce_bounty_task_deadline()")
    op.execute("ALTER TABLE bounties DROP CONSTRAINT IF EXISTS bounties_proof_requirements_unique")
    op.execute("DROP FUNCTION IF EXISTS hah_jsonb_text_array_is_unique(jsonb)")
