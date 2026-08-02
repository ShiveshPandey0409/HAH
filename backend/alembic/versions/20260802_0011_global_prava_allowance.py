"""Reuse one approved Prava mandate across reserved task budgets.

Revision ID: 20260802_0011
Revises: 20260802_0010
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0011"
down_revision: str | None = "20260802_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_authorizations",
        sa.Column("pool_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "payment_authorizations",
        sa.Column("pool_cap_minor", sa.BigInteger(), nullable=True),
    )
    op.execute(
        """
        UPDATE payment_authorizations
           SET pool_id = gen_random_uuid(),
               pool_cap_minor = total_cap_minor
        """
    )
    op.alter_column("payment_authorizations", "pool_id", nullable=False)
    op.alter_column("payment_authorizations", "pool_cap_minor", nullable=False)
    op.alter_column(
        "payment_authorizations",
        "pool_id",
        server_default=sa.text("gen_random_uuid()"),
    )
    op.drop_constraint(
        "payment_authorizations_provider_authorization_ref_key",
        "payment_authorizations",
        type_="unique",
    )
    op.create_index(
        "payment_authorizations_pool_idx",
        "payment_authorizations",
        ["pool_id", "created_at"],
    )
    op.create_check_constraint(
        "payment_authorizations_pool_cap_check",
        "payment_authorizations",
        "total_cap_minor <= pool_cap_minor",
    )
    op.create_table_comment(
        "payment_authorizations",
        "Task reservation against a reusable HAH-level Prava allowance pool.",
        existing_comment="Task-level Prava standing mandate and local automatic-payment caps.",
    )
    _create_pool_allocation_guard()


def downgrade() -> None:
    op.execute("DROP TRIGGER payment_authorizations_validate_pool ON payment_authorizations")
    op.execute("DROP FUNCTION hah_validate_payment_pool_allocation()")
    op.create_table_comment(
        "payment_authorizations",
        "Task-level Prava standing mandate and local automatic-payment caps.",
        existing_comment="Task reservation against a reusable HAH-level Prava allowance pool.",
    )
    op.drop_constraint(
        "payment_authorizations_pool_cap_check",
        "payment_authorizations",
        type_="check",
    )
    op.drop_index("payment_authorizations_pool_idx", table_name="payment_authorizations")
    # A downgraded schema cannot represent a shared provider mandate. Preserve
    # the oldest task's reference and make later task rows require re-approval.
    op.execute(
        """
        WITH ranked AS (
          SELECT id,
                 row_number() OVER (
                   PARTITION BY provider_authorization_ref ORDER BY created_at, id
                 ) AS position
            FROM payment_authorizations
           WHERE provider_authorization_ref IS NOT NULL
        )
        UPDATE payment_authorizations AS payment_authorization
           SET provider_authorization_ref = NULL,
               status = 'pending'::authorization_status
          FROM ranked
         WHERE payment_authorization.id = ranked.id
           AND ranked.position > 1
        """
    )
    op.create_unique_constraint(
        "payment_authorizations_provider_authorization_ref_key",
        "payment_authorizations",
        ["provider_authorization_ref"],
    )
    op.drop_column("payment_authorizations", "pool_cap_minor")
    op.drop_column("payment_authorizations", "pool_id")


def _create_pool_allocation_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_validate_payment_pool_allocation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_allocated bigint;
          v_pool_cap bigint;
          v_currency char(3);
          v_customer text;
          v_mandate text;
        BEGIN
          -- Serialize every reservation into the same logical pool. This
          -- prevents two API or MCP requests from oversubscribing it.
          PERFORM pg_advisory_xact_lock(hashtextextended(NEW.pool_id::text, 0));

          SELECT COALESCE(sum(total_cap_minor), 0),
                 min(pool_cap_minor),
                 min(currency),
                 min(provider_customer_ref),
                 min(provider_authorization_ref)
            INTO v_allocated, v_pool_cap, v_currency, v_customer, v_mandate
            FROM payment_authorizations
           WHERE pool_id = NEW.pool_id
             AND id <> NEW.id;

          IF v_allocated + NEW.total_cap_minor > NEW.pool_cap_minor THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HPA01',
              MESSAGE = 'HAH Prava allowance pool is fully allocated';
          END IF;
          IF v_pool_cap IS NOT NULL AND v_pool_cap <> NEW.pool_cap_minor THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HPA02',
              MESSAGE = 'HAH Prava allowance pool cap does not match';
          END IF;
          IF v_currency IS NOT NULL AND v_currency <> NEW.currency THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HPA03',
              MESSAGE = 'HAH Prava allowance pool currency does not match';
          END IF;
          IF v_customer IS NOT NULL
             AND NEW.provider_customer_ref IS DISTINCT FROM v_customer THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HPA04',
              MESSAGE = 'HAH Prava allowance pool payer does not match';
          END IF;
          IF v_mandate IS NOT NULL
             AND NEW.provider_authorization_ref IS NOT NULL
             AND NEW.provider_authorization_ref <> v_mandate THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HPA05',
              MESSAGE = 'HAH Prava allowance pool mandate does not match';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER payment_authorizations_validate_pool
        BEFORE INSERT OR UPDATE ON payment_authorizations
        FOR EACH ROW EXECUTE FUNCTION hah_validate_payment_pool_allocation()
        """
    )
