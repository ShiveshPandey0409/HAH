"""Add Prava mandate automation and append-only internal wallet credits.

Revision ID: 20260802_0009
Revises: 20260802_0008
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0009"
down_revision: str | None = "20260802_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _extend_payment_authorizations()
    _guard_authorized_task_definition()
    _add_payment_retry_state()
    _add_wallet_ledger()
    _update_payment_comments()
    _harden_payment_audits()
    _harden_payment_state_machine()
    _enable_payment_oauth_scopes()


def downgrade() -> None:
    _restore_oauth_scopes()
    op.execute("DROP TRIGGER IF EXISTS bounties_guard_payment_definition ON bounties")
    op.execute("DROP FUNCTION IF EXISTS hah_guard_authorized_bounty_definition()")
    op.execute("DROP TRIGGER IF EXISTS tasks_guard_payment_definition ON tasks")
    op.execute("DROP FUNCTION IF EXISTS hah_guard_authorized_task_definition()")
    op.create_table_comment(
        "payment_attempts",
        "Retry/audit trail for calls to the payment provider; never store PAN/CVV.",
        existing_comment="Safe Prava retry audit; card credentials are prohibited.",
    )
    op.create_table_comment(
        "payments",
        "One idempotent logical payout for an approved claim.",
        existing_comment="One idempotent Prava-funded internal reward payment.",
    )
    op.create_table_comment(
        "payment_authorizations",
        "Prava authorization and limits for automatic task payouts.",
        existing_comment="Task-level Prava standing mandate and local automatic-payment caps.",
    )
    op.execute("DROP TRIGGER wallet_entries_immutable ON wallet_entries")
    op.execute("DROP FUNCTION hah_guard_wallet_entry_mutation()")
    op.execute("DROP TRIGGER wallet_entries_validate ON wallet_entries")
    op.execute("DROP FUNCTION hah_validate_wallet_entry()")
    op.execute("DROP TRIGGER payments_guard_state ON payments")
    op.execute("DROP FUNCTION hah_guard_payment_state()")
    op.drop_constraint(
        "payment_attempts_response_sensitive_check",
        "payment_attempts",
        type_="check",
    )
    op.drop_constraint(
        "payment_attempts_request_sensitive_check",
        "payment_attempts",
        type_="check",
    )
    op.execute("DROP FUNCTION hah_payment_json_has_sensitive_key(jsonb)")
    op.drop_index("wallet_entries_user_currency_idx", table_name="wallet_entries")
    op.drop_table("wallet_entries")
    op.drop_index("payments_due_idx", table_name="payments")
    op.drop_column("payments", "next_attempt_at")
    op.drop_constraint(
        "payment_authorizations_session_ref_check",
        "payment_authorizations",
        type_="check",
    )
    op.drop_constraint(
        "payment_authorizations_customer_ref_check",
        "payment_authorizations",
        type_="check",
    )
    op.drop_constraint(
        "payment_authorizations_provider_session_ref_key",
        "payment_authorizations",
        type_="unique",
    )
    op.drop_constraint(
        "payment_authorizations_provider_funding_transaction_ref_key",
        "payment_authorizations",
        type_="unique",
    )
    op.drop_constraint(
        "payment_authorizations_funding_idempotency_key_key",
        "payment_authorizations",
        type_="unique",
    )
    op.drop_constraint(
        "payment_authorizations_task_id_key",
        "payment_authorizations",
        type_="unique",
    )
    op.drop_column("payment_authorizations", "provider_session_expires_at")
    op.drop_column("payment_authorizations", "provider_session_ref")
    op.drop_column("payment_authorizations", "provider_customer_ref")
    op.drop_column("payment_authorizations", "funded_at")
    op.drop_column("payment_authorizations", "funding_failure_message")
    op.drop_column("payment_authorizations", "funding_failure_code")
    op.drop_column("payment_authorizations", "provider_funding_transaction_ref")
    op.drop_column("payment_authorizations", "funding_idempotency_key")
    op.drop_column("payment_authorizations", "funding_status")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION apply_payment_success()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_auth payment_authorizations%ROWTYPE;
        BEGIN
          IF NEW.status = 'succeeded' AND OLD.status IS DISTINCT FROM NEW.status THEN
            SELECT * INTO v_auth FROM payment_authorizations
             WHERE id = NEW.authorization_id FOR UPDATE;
            IF v_auth.status <> 'active'
               OR v_auth.used_minor + NEW.amount_minor > v_auth.total_cap_minor
               OR (v_auth.max_payments IS NOT NULL
                   AND v_auth.payments_used >= v_auth.max_payments)
               OR (v_auth.valid_until IS NOT NULL AND v_auth.valid_until <= now()) THEN
              RAISE EXCEPTION 'payment success would exceed or violate its authorization';
            END IF;
            UPDATE bounty_claims
               SET status = 'paid', paid_at = COALESCE(NEW.completed_at, now())
             WHERE id = NEW.claim_id AND status = 'approved';
            UPDATE payment_authorizations
               SET used_minor = used_minor + NEW.amount_minor,
                   payments_used = payments_used + 1
             WHERE id = NEW.authorization_id;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )


def _extend_payment_authorizations() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM payment_authorizations GROUP BY task_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HMG01',
              MESSAGE = 'duplicate task payment authorizations must be resolved';
          END IF;
        END;
        $$
        """
    )
    op.add_column("payment_authorizations", sa.Column("provider_customer_ref", sa.Text()))
    op.add_column("payment_authorizations", sa.Column("provider_session_ref", sa.Text()))
    op.add_column(
        "payment_authorizations",
        sa.Column("provider_session_expires_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "payment_authorizations",
        sa.Column(
            "funding_status",
            sa.Enum(name="payment_status", create_type=False),
            nullable=False,
            server_default=sa.text("'created'::payment_status"),
        ),
    )
    op.add_column(
        "payment_authorizations",
        sa.Column("funding_idempotency_key", sa.Text()),
    )
    op.execute(
        """
        UPDATE payment_authorizations
           SET funding_idempotency_key = 'hah-task-funding:' || task_id::text
        """
    )
    op.alter_column("payment_authorizations", "funding_idempotency_key", nullable=False)
    op.add_column(
        "payment_authorizations",
        sa.Column("provider_funding_transaction_ref", sa.Text()),
    )
    op.add_column(
        "payment_authorizations",
        sa.Column("funding_failure_code", sa.Text()),
    )
    op.add_column(
        "payment_authorizations",
        sa.Column("funding_failure_message", sa.Text()),
    )
    op.add_column(
        "payment_authorizations",
        sa.Column("funded_at", sa.DateTime(timezone=True)),
    )
    op.create_unique_constraint(
        "payment_authorizations_task_id_key",
        "payment_authorizations",
        ["task_id"],
    )
    op.create_unique_constraint(
        "payment_authorizations_provider_session_ref_key",
        "payment_authorizations",
        ["provider_session_ref"],
    )
    op.create_unique_constraint(
        "payment_authorizations_funding_idempotency_key_key",
        "payment_authorizations",
        ["funding_idempotency_key"],
    )
    op.create_unique_constraint(
        "payment_authorizations_provider_funding_transaction_ref_key",
        "payment_authorizations",
        ["provider_funding_transaction_ref"],
    )
    op.create_check_constraint(
        "payment_authorizations_customer_ref_check",
        "payment_authorizations",
        """
        provider_customer_ref IS NULL
        OR (provider_customer_ref = btrim(provider_customer_ref)
            AND provider_customer_ref <> '')
        """,
    )
    op.create_check_constraint(
        "payment_authorizations_session_ref_check",
        "payment_authorizations",
        """
        provider_session_ref IS NULL
        OR (provider_session_ref = btrim(provider_session_ref)
            AND provider_session_ref <> '')
        """,
    )


def _add_payment_retry_state() -> None:
    op.add_column("payments", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE payments SET next_attempt_at = now() WHERE status IN ('created', 'failed')")
    op.create_index(
        "payments_due_idx",
        "payments",
        ["next_attempt_at", "created_at"],
        postgresql_where=sa.text("status = 'created'"),
    )


def _guard_authorized_task_definition() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_guard_authorized_task_definition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM payment_authorizations WHERE task_id = OLD.id
          ) AND ROW(
            NEW.title, NEW.description, NEW.total_budget_minor, NEW.currency
          ) IS DISTINCT FROM ROW(
            OLD.title, OLD.description, OLD.total_budget_minor, OLD.currency
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01',
              MESSAGE = 'Prava-authorized task definition is immutable';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tasks_guard_payment_definition
        BEFORE UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION hah_guard_authorized_task_definition()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_authorized_bounty_definition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_task_id uuid;
          v_changed boolean;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            v_task_id := NEW.task_id;
            v_changed := true;
          ELSIF TG_OP = 'DELETE' THEN
            v_task_id := OLD.task_id;
            v_changed := true;
          ELSE
            v_task_id := OLD.task_id;
            v_changed := ROW(
              NEW.task_id, NEW.reward_minor, NEW.slots_total
            ) IS DISTINCT FROM ROW(
              OLD.task_id, OLD.reward_minor, OLD.slots_total
            );
          END IF;
          IF v_changed AND EXISTS (
            SELECT 1 FROM payment_authorizations WHERE task_id = v_task_id
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01',
              MESSAGE = 'Prava-authorized bounty budget is immutable';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER bounties_guard_payment_definition
        BEFORE INSERT OR UPDATE OR DELETE ON bounties
        FOR EACH ROW EXECUTE FUNCTION hah_guard_authorized_bounty_definition()
        """
    )


def _add_wallet_ledger() -> None:
    op.create_table(
        "wallet_entries",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "payment_id",
            sa.Uuid(),
            sa.ForeignKey("payments.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column(
            "entry_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'task_reward'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount_minor > 0", name="wallet_entries_amount_check"),
        sa.CheckConstraint("currency = upper(currency)", name="wallet_entries_currency_check"),
        sa.CheckConstraint(
            "entry_type = 'task_reward'",
            name="wallet_entries_type_check",
        ),
        comment="Append-only internal hackathon wallet credits; no redemption.",
    )
    op.create_index(
        "wallet_entries_user_currency_idx",
        "wallet_entries",
        ["user_id", "currency", "created_at"],
    )


def _update_payment_comments() -> None:
    op.create_table_comment(
        "payment_authorizations",
        "Task-level Prava standing mandate and local automatic-payment caps.",
        existing_comment="Prava authorization and limits for automatic task payouts.",
    )
    op.create_table_comment(
        "payments",
        "One idempotent Prava-funded internal reward payment.",
        existing_comment="One idempotent logical payout for an approved claim.",
    )
    op.create_table_comment(
        "payment_attempts",
        "Safe Prava retry audit; card credentials are prohibited.",
        existing_comment=(
            "Retry/audit trail for calls to the payment provider; never store PAN/CVV."
        ),
    )


def _harden_payment_audits() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_payment_json_has_sensitive_key(document jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE
          item record;
          child jsonb;
          normalized_key text;
        BEGIN
          IF document IS NULL THEN RETURN false; END IF;
          IF jsonb_typeof(document) = 'object' THEN
            FOR item IN SELECT key, value FROM jsonb_each(document) LOOP
              normalized_key := lower(regexp_replace(item.key, '[^a-z0-9]', '', 'g'));
              IF normalized_key = ANY(ARRAY[
                'cardnumber', 'pan', 'token', 'cvv', 'cvc', 'dynamiccvv',
                'securitycode', 'expiry', 'expirydate', 'expirymonth', 'expiryyear',
                'encryptedpayload'
              ]) THEN
                RETURN true;
              END IF;
              IF hah_payment_json_has_sensitive_key(item.value) THEN RETURN true; END IF;
            END LOOP;
          ELSIF jsonb_typeof(document) = 'array' THEN
            FOR child IN SELECT value FROM jsonb_array_elements(document) LOOP
              IF hah_payment_json_has_sensitive_key(child) THEN RETURN true; END IF;
            END LOOP;
          END IF;
          RETURN false;
        END;
        $$
        """
    )
    op.create_check_constraint(
        "payment_attempts_request_sensitive_check",
        "payment_attempts",
        "NOT hah_payment_json_has_sensitive_key(request_data)",
    )
    op.create_check_constraint(
        "payment_attempts_response_sensitive_check",
        "payment_attempts",
        "NOT hah_payment_json_has_sensitive_key(response_data)",
    )


def _harden_payment_state_machine() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_guard_payment_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(
            NEW.authorization_id, NEW.task_id, NEW.bounty_id, NEW.claim_id,
            NEW.submission_id, NEW.payer_user_id, NEW.payee_user_id, NEW.provider,
            NEW.amount_minor, NEW.currency, NEW.idempotency_key, NEW.created_at
          ) IS DISTINCT FROM ROW(
            OLD.authorization_id, OLD.task_id, OLD.bounty_id, OLD.claim_id,
            OLD.submission_id, OLD.payer_user_id, OLD.payee_user_id, OLD.provider,
            OLD.amount_minor, OLD.currency, OLD.idempotency_key, OLD.created_at
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'payment identity is immutable';
          END IF;
          IF OLD.status IN ('succeeded', 'cancelled') AND ROW(
            NEW.status, NEW.provider_transaction_ref, NEW.failure_code,
            NEW.failure_message, NEW.next_attempt_at, NEW.completed_at
          ) IS DISTINCT FROM ROW(
            OLD.status, OLD.provider_transaction_ref, OLD.failure_code,
            OLD.failure_message, OLD.next_attempt_at, OLD.completed_at
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'terminal payment is immutable';
          END IF;
          IF NEW.provider_transaction_ref IS DISTINCT FROM OLD.provider_transaction_ref
             AND OLD.provider_transaction_ref IS NOT NULL THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'provider transaction reference is immutable';
          END IF;
          IF NEW.status = 'succeeded' AND (
               NEW.completed_at IS NULL
               OR NEW.next_attempt_at IS NOT NULL OR NEW.failure_code IS NOT NULL
               OR NEW.failure_message IS NOT NULL
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'HVL01', MESSAGE = 'invalid succeeded payment';
          END IF;
          IF NEW.status IN ('failed', 'cancelled') AND (
               NEW.completed_at IS NULL OR NEW.next_attempt_at IS NOT NULL
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'HVL01', MESSAGE = 'invalid terminal payment';
          END IF;
          IF NEW.status IN ('created', 'processing') AND NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'HVL01', MESSAGE = 'unfinished payment completed';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER payments_guard_state
        BEFORE UPDATE ON payments
        FOR EACH ROW EXECUTE FUNCTION hah_guard_payment_state()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_validate_wallet_entry()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM payments payment
             WHERE payment.id = NEW.payment_id
               AND payment.status = 'succeeded'
               AND payment.payee_user_id = NEW.user_id
               AND payment.amount_minor = NEW.amount_minor
               AND payment.currency = NEW.currency
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'wallet credit must match a succeeded payment';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wallet_entries_validate
        BEFORE INSERT ON wallet_entries
        FOR EACH ROW EXECUTE FUNCTION hah_validate_wallet_entry()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_wallet_entry_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'wallet ledger is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wallet_entries_immutable
        BEFORE UPDATE OR DELETE ON wallet_entries
        FOR EACH ROW EXECUTE FUNCTION hah_guard_wallet_entry_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION apply_payment_success()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_auth payment_authorizations%ROWTYPE;
        BEGIN
          IF NEW.status = 'succeeded' AND OLD.status IS DISTINCT FROM NEW.status THEN
            SELECT * INTO v_auth FROM payment_authorizations
             WHERE id = NEW.authorization_id FOR UPDATE;
            IF v_auth.status <> 'active'
               OR v_auth.funding_status <> 'succeeded'
               OR v_auth.used_minor + NEW.amount_minor > v_auth.total_cap_minor
               OR (v_auth.max_payments IS NOT NULL
                   AND v_auth.payments_used >= v_auth.max_payments)
               OR (v_auth.valid_until IS NOT NULL AND v_auth.valid_until <= now()) THEN
              RAISE EXCEPTION 'payment success would exceed or violate its authorization';
            END IF;
            UPDATE bounty_claims
               SET status = 'paid', paid_at = COALESCE(NEW.completed_at, now())
             WHERE id = NEW.claim_id AND status = 'approved';
            IF NOT FOUND THEN
              RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'claim is not payable';
            END IF;
            INSERT INTO wallet_entries (
              user_id, payment_id, amount_minor, currency, entry_type
            ) VALUES (
              NEW.payee_user_id, NEW.id, NEW.amount_minor, NEW.currency, 'task_reward'
            );
            UPDATE payment_authorizations
               SET used_minor = used_minor + NEW.amount_minor,
                   payments_used = payments_used + 1
             WHERE id = NEW.authorization_id;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )


def _enable_payment_oauth_scopes() -> None:
    op.drop_constraint(
        "oauth_delegations_supported_scopes_check",
        "oauth_delegations",
        type_="check",
    )
    op.create_check_constraint(
        "oauth_delegations_supported_scopes_check",
        "oauth_delegations",
        """
        approved_scopes <@ ARRAY[
          'mcp:access', 'tasks:create', 'submissions:read',
          'submissions:verify', 'submissions:approve',
          'payments:read', 'payments:write'
        ]::text[]
        AND approved_scopes @> ARRAY['mcp:access']::text[]
        """,
    )


def _restore_oauth_scopes() -> None:
    op.drop_constraint(
        "oauth_delegations_supported_scopes_check",
        "oauth_delegations",
        type_="check",
    )
    op.create_check_constraint(
        "oauth_delegations_supported_scopes_check",
        "oauth_delegations",
        """
        approved_scopes <@ ARRAY[
          'mcp:access', 'tasks:create', 'submissions:read',
          'submissions:verify', 'submissions:approve'
        ]::text[]
        AND approved_scopes @> ARRAY['mcp:access']::text[]
        """,
    )
