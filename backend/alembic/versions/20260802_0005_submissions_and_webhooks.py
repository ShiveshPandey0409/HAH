"""Add submission verification and webhook delivery guarantees.

Revision ID: 20260802_0005
Revises: 20260802_0004
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0005"
down_revision: str | None = "20260802_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_mcp_submission_link()
    _scrub_legacy_mcp_requests()
    _strengthen_submission_tables()
    _strengthen_submission_state_machine()
    _freeze_claimed_proof_requirements()
    _strengthen_webhook_tables()


def downgrade() -> None:
    _restore_webhook_baseline()
    _restore_claimed_bounty_guard()
    _restore_submission_state_machine()
    _restore_submission_tables()
    _drop_mcp_submission_link()


def _add_mcp_submission_link() -> None:
    op.add_column("mcp_requests", sa.Column("submission_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "mcp_requests_submission_id_fkey",
        "mcp_requests",
        "submissions",
        ["submission_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("mcp_requests_submission_id_idx", "mcp_requests", ["submission_id"])


def _scrub_legacy_mcp_requests() -> None:
    # Pre-0005 task audits copied complete task inputs and responses. Keep only
    # bounded structural ownership/resource markers; those idempotency keys remain
    # reserved but intentionally cannot replay after their unsafe payload is removed.
    op.execute(
        """
        UPDATE mcp_requests AS request
           SET status = CASE
                 WHEN request.status = 'started' THEN 'failed'::request_status
                 ELSE request.status
               END,
               request_data = jsonb_strip_nulls(jsonb_build_object(
                 'legacy_redacted', true,
                 'creator_id', client.creator_id::text,
                 'method', request.method,
                 'task_id', request.task_id::text
               )),
               response_data = CASE
                 WHEN request.status = 'succeeded' THEN jsonb_strip_nulls(
                   jsonb_build_object(
                     'legacy_redacted', true,
                     'replayable', false,
                     'task_id', request.task_id::text
                   )
                 )
                 ELSE NULL
               END,
               error_message = CASE
                 WHEN request.status = 'started' THEN 'legacy MCP request interrupted'
                 WHEN request.status = 'failed' THEN 'legacy MCP request failed'
                 ELSE NULL
               END,
               completed_at = COALESCE(request.completed_at, now())
          FROM api_clients AS client
         WHERE client.id = request.api_client_id
        """
    )


def _strengthen_submission_tables() -> None:
    op.drop_constraint("submissions_check", "submissions", type_="check")
    op.execute(
        """
        UPDATE submissions
           SET verification_method = CASE
                 WHEN verification_status = 'pending' THEN NULL
                 WHEN verification_status = 'review_required'
                   THEN COALESCE(verification_method, 'manual')
                 ELSE verification_method
               END,
               verified_at = CASE
                 WHEN verification_status IN ('pending', 'review_required') THEN NULL
                 ELSE verified_at
               END
         WHERE verification_status IN ('pending', 'review_required')
        """
    )
    op.create_check_constraint(
        "submissions_verification_state_check",
        "submissions",
        """
        (verification_status = 'pending'
          AND verification_method IS NULL
          AND verified_at IS NULL)
        OR
        (verification_status = 'review_required'
          AND verification_method IS NOT NULL
          AND verified_at IS NULL)
        OR
        (verification_status IN ('passed', 'failed')
          AND verification_method IS NOT NULL
          AND verified_at IS NOT NULL)
        """,
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM submission_proofs
             GROUP BY submission_id, kind
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HMG01',
              MESSAGE = 'duplicate submission proof kinds must be resolved before migration';
          END IF;
        END;
        $$
        """
    )
    op.create_unique_constraint(
        "submission_proofs_submission_id_kind_key",
        "submission_proofs",
        ["submission_id", "kind"],
    )
    op.drop_constraint("submission_proofs_check", "submission_proofs", type_="check")
    op.execute(
        """
        ALTER TABLE submission_proofs
          ADD CONSTRAINT submission_proofs_shape_check CHECK (
            (kind = 'url'
              AND external_url IS NOT NULL
              AND storage_key IS NULL
              AND mime_type IS NULL
              AND sha256 IS NULL)
            OR
            (kind IN ('screenshot', 'image')
              AND external_url IS NULL
              AND storage_key IS NOT NULL)
          ) NOT VALID,
          ADD CONSTRAINT submission_proofs_https_url_check
            CHECK (external_url IS NULL OR external_url ~* '^https://[^[:space:]]+$')
            NOT VALID,
          ADD CONSTRAINT submission_proofs_storage_key_nonblank_check
            CHECK (storage_key IS NULL OR btrim(storage_key) <> '') NOT VALID,
          ADD CONSTRAINT submission_proofs_sha256_check
            CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$') NOT VALID
        """
    )


def _strengthen_submission_state_machine() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_validate_submission_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_claim bounty_claims%ROWTYPE;
          v_expected_revision integer;
        BEGIN
          SELECT * INTO v_claim
            FROM bounty_claims
           WHERE id = NEW.claim_id
           FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'claim not found';
          END IF;
          IF v_claim.status NOT IN ('claimed', 'changes_requested') THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'claim cannot accept a submission';
          END IF;
          IF v_claim.status = 'claimed'
             AND v_claim.claim_expires_at IS NOT NULL
             AND v_claim.claim_expires_at <= now() THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'claim reservation has expired';
          END IF;
          IF NEW.verification_status <> 'pending'
             OR NEW.verification_method IS NOT NULL
             OR NEW.verifier_user_id IS NOT NULL
             OR NEW.verification_note IS NOT NULL
             OR NEW.verified_at IS NOT NULL
             OR NEW.verification_checks <> '{}'::jsonb THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'a submission must begin pending verification';
          END IF;

          SELECT COALESCE(max(revision), 0) + 1 INTO v_expected_revision
            FROM submissions
           WHERE claim_id = NEW.claim_id;
          IF NEW.revision <> v_expected_revision THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'submission revision is not the next revision';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER submissions_validate_insert
        BEFORE INSERT ON submissions
        FOR EACH ROW EXECUTE FUNCTION hah_validate_submission_insert()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION apply_submission_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE bounty_claims
             SET status = 'submitted', submitted_at = NEW.submitted_at
           WHERE id = NEW.claim_id
             AND status IN ('claimed', 'changes_requested');
          IF NOT FOUND THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'claim cannot accept a submission';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_submission_verification()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.claim_id IS DISTINCT FROM OLD.claim_id
             OR NEW.revision IS DISTINCT FROM OLD.revision
             OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'submission identity is immutable';
          END IF;
          IF OLD.verification_status IN ('passed', 'failed')
             AND ROW(
               NEW.verification_method,
               NEW.verification_status,
               NEW.verification_checks,
               NEW.verifier_user_id,
               NEW.verification_note,
               NEW.verified_at
             ) IS DISTINCT FROM ROW(
               OLD.verification_method,
               OLD.verification_status,
               OLD.verification_checks,
               OLD.verifier_user_id,
               OLD.verification_note,
               OLD.verified_at
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'final verification result is immutable';
          END IF;
          IF OLD.verification_status = 'review_required'
             AND NEW.verification_status = 'pending' THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'verification cannot return to pending';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER submissions_guard_verification
        BEFORE UPDATE OF claim_id, revision, submitted_at, verification_method,
          verification_status, verification_checks, verifier_user_id,
          verification_note, verified_at
        ON submissions
        FOR EACH ROW EXECUTE FUNCTION hah_guard_submission_verification()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION apply_verification_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_latest_revision integer;
        BEGIN
          SELECT max(revision) INTO v_latest_revision
            FROM submissions
           WHERE claim_id = NEW.claim_id;
          IF NEW.revision <> v_latest_revision THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'only the latest submission can be verified';
          END IF;

          IF NEW.verification_status = 'passed' THEN
            IF EXISTS (
              SELECT 1
                FROM submission_proofs p
               WHERE p.submission_id = NEW.id
                 AND NOT (
                   (p.kind = 'url'
                     AND p.external_url IS NOT NULL
                     AND p.external_url ~* '^https://[^[:space:]]+$'
                     AND p.storage_key IS NULL
                     AND p.mime_type IS NULL
                     AND p.sha256 IS NULL)
                   OR
                   (p.kind IN ('screenshot', 'image')
                     AND p.external_url IS NULL
                     AND p.storage_key IS NOT NULL
                     AND btrim(p.storage_key) <> ''
                     AND (p.sha256 IS NULL OR p.sha256 ~ '^[0-9a-f]{64}$'))
                 )
            ) THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01', MESSAGE = 'submission contains an invalid proof';
            END IF;
            IF EXISTS (
              SELECT 1
                FROM bounty_claims c
                JOIN bounties b ON b.id = c.bounty_id
                CROSS JOIN LATERAL
                  jsonb_array_elements_text(b.proof_requirements) required(kind)
               WHERE c.id = NEW.claim_id
                 AND NOT EXISTS (
                   SELECT 1 FROM submission_proofs p
                    WHERE p.submission_id = NEW.id AND p.kind = required.kind
                 )
            ) THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01', MESSAGE = 'submission is missing a required proof type';
            END IF;
            UPDATE bounty_claims
               SET status = 'approved', approved_at = NEW.verified_at
             WHERE id = NEW.claim_id AND status IN ('submitted', 'reviewing');
          ELSIF NEW.verification_status = 'failed' THEN
            UPDATE bounty_claims
               SET status = 'rejected'
             WHERE id = NEW.claim_id AND status IN ('submitted', 'reviewing');
          ELSIF NEW.verification_status = 'review_required' THEN
            UPDATE bounty_claims
               SET status = 'reviewing'
             WHERE id = NEW.claim_id AND status = 'submitted';
          ELSE
            RETURN NEW;
          END IF;

          IF NOT FOUND THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'claim cannot accept this verification result';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_submission_proofs()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_submission_id uuid;
          v_status verification_status;
        BEGIN
          IF TG_OP = 'UPDATE'
             AND NEW.submission_id IS DISTINCT FROM OLD.submission_id THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'proof submission ownership is immutable';
          END IF;
          v_submission_id := CASE
            WHEN TG_OP = 'DELETE' THEN OLD.submission_id ELSE NEW.submission_id
          END;
          SELECT verification_status INTO v_status
            FROM submissions
           WHERE id = v_submission_id
           FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'submission not found';
          END IF;
          IF v_status <> 'pending' THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'proofs are immutable after review begins';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER submission_proofs_guard_changes
        BEFORE INSERT OR UPDATE OR DELETE ON submission_proofs
        FOR EACH ROW EXECUTE FUNCTION hah_guard_submission_proofs()
        """
    )


def _freeze_claimed_proof_requirements() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_claimed_bounty_pricing_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (NEW.task_id IS DISTINCT FROM OLD.task_id
              OR NEW.reward_minor IS DISTINCT FROM OLD.reward_minor
              OR NEW.proof_requirements IS DISTINCT FROM OLD.proof_requirements)
             AND EXISTS (SELECT 1 FROM bounty_claims WHERE bounty_id = OLD.id) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'claimed bounty pricing, task, and proof requirements cannot change';
          END IF;
          IF NEW.status = 'cancelled'
             AND NEW.status IS DISTINCT FROM OLD.status
             AND EXISTS (
               SELECT 1
                 FROM bounty_claims
                WHERE bounty_id = OLD.id
                  AND hah_claim_occupies_slot(status, claim_expires_at)
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'bounty with active claims cannot be cancelled';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER bounties_claimed_pricing_immutable ON bounties")
    op.execute(
        """
        CREATE TRIGGER bounties_claimed_pricing_immutable
        BEFORE UPDATE OF task_id, reward_minor, proof_requirements, status ON bounties
        FOR EACH ROW EXECUTE FUNCTION prevent_claimed_bounty_pricing_change()
        """
    )


def _strengthen_webhook_tables() -> None:
    op.add_column("webhook_endpoints", sa.Column("secret_ciphertext", sa.LargeBinary()))
    # Baseline endpoints have no recoverable signing secret and cannot be delivered.
    # Disable them and scrub capability URLs; a downgrade cannot reconstruct that data.
    op.execute(
        """
        UPDATE webhook_endpoints
           SET subscribed_events = COALESCE(
             ARRAY(
               SELECT DISTINCT CASE event
                 WHEN 'submission.approved' THEN 'verification.completed'
                 ELSE event
               END
                 FROM unnest(subscribed_events) AS event
                WHERE CASE event
                  WHEN 'submission.approved' THEN 'verification.completed'
                  ELSE event
                END = ANY (ARRAY[
                  'submission.created',
                  'verification.completed',
                  'mcp_request.completed',
                  'payment.succeeded',
                  'payment.failed'
                ])
                ORDER BY 1
             ),
             ARRAY[]::text[]
           ),
               url = 'https://encrypted.invalid/',
               status = CASE
                 WHEN secret_ciphertext IS NULL THEN 'disabled'::integration_status
                 ELSE status
               END
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_text_array_is_unique(value text[])
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $$
          SELECT cardinality(value) = count(DISTINCT item)
            FROM unnest(value) AS items(item)
        $$
        """
    )
    op.create_check_constraint(
        "webhook_endpoints_active_secret_check",
        "webhook_endpoints",
        "status <> 'active' OR secret_ciphertext IS NOT NULL",
    )
    op.create_check_constraint(
        "webhook_endpoints_encrypted_url_check",
        "webhook_endpoints",
        "url = 'https://encrypted.invalid/'",
    )
    op.create_check_constraint(
        "webhook_endpoints_subscribed_events_check",
        "webhook_endpoints",
        """
        subscribed_events <@ ARRAY[
          'submission.created',
          'verification.completed',
          'mcp_request.completed',
          'payment.succeeded',
          'payment.failed'
        ]::text[]
        AND hah_text_array_is_unique(subscribed_events)
        AND array_position(subscribed_events, NULL) IS NULL
        """,
    )
    op.create_index(
        "webhook_endpoints_creator_active_key",
        "webhook_endpoints",
        ["creator_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.execute(
        """
        CREATE TRIGGER webhook_endpoints_validate_creator
        BEFORE INSERT OR UPDATE OF creator_id ON webhook_endpoints
        FOR EACH ROW EXECUTE FUNCTION validate_creator()
        """
    )

    op.add_column("webhook_deliveries", sa.Column("deduplication_key", sa.Text()))
    op.add_column("webhook_deliveries", sa.Column("payload_body", sa.LargeBinary()))
    op.add_column("webhook_deliveries", sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("webhook_deliveries", sa.Column("last_response_code", sa.Integer()))
    op.add_column("webhook_deliveries", sa.Column("last_response_body", sa.Text()))
    op.add_column("webhook_deliveries", sa.Column("failed_at", sa.DateTime(timezone=True)))
    op.add_column("webhook_deliveries", sa.Column("lease_token", sa.Uuid()))
    op.add_column("webhook_deliveries", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE webhook_deliveries
           SET event_type = CASE event_type
                 WHEN 'submission.approved' THEN 'verification.completed'
                 ELSE event_type
               END,
               deduplication_key = 'legacy:' || id::text,
               payload = jsonb_build_object(
                 'reason', 'legacy_delivery_scrubbed',
                 'redacted', true
               ),
               payload_body = convert_to(
                 jsonb_build_object(
                   'reason', 'legacy_delivery_scrubbed',
                   'redacted', true
                 )::text,
                 'UTF8'
               ),
               last_error = NULL,
               next_attempt_at = CASE
                 WHEN status IN ('pending', 'retrying')
                   THEN COALESCE(next_attempt_at, created_at)
                 ELSE NULL
               END,
               delivered_at = CASE
                 WHEN status = 'delivered' THEN COALESCE(delivered_at, created_at)
                 ELSE NULL
               END,
               failed_at = CASE
                 WHEN status = 'failed' THEN created_at
                 ELSE NULL
               END
        """
    )
    op.execute(
        """
        UPDATE webhook_deliveries AS delivery
           SET status = 'failed',
               failed_at = now(),
               next_attempt_at = NULL,
               last_error = 'endpoint_secret_unavailable'
          FROM webhook_endpoints AS endpoint
         WHERE endpoint.id = delivery.endpoint_id
           AND endpoint.secret_ciphertext IS NULL
           AND delivery.status IN ('pending', 'retrying')
        """
    )
    op.alter_column("webhook_deliveries", "deduplication_key", nullable=False)
    op.alter_column("webhook_deliveries", "payload_body", nullable=False)
    op.create_unique_constraint(
        "webhook_deliveries_endpoint_id_deduplication_key_key",
        "webhook_deliveries",
        ["endpoint_id", "deduplication_key"],
    )
    op.create_check_constraint(
        "webhook_deliveries_deduplication_key_check",
        "webhook_deliveries",
        "length(deduplication_key) BETWEEN 1 AND 255",
    )
    op.execute(
        """
        ALTER TABLE webhook_deliveries
          ADD CONSTRAINT webhook_deliveries_payload_body_size_check
          CHECK (octet_length(payload_body) BETWEEN 1 AND 65536) NOT VALID
        """
    )
    op.create_check_constraint(
        "webhook_deliveries_response_code_check",
        "webhook_deliveries",
        "last_response_code IS NULL OR last_response_code BETWEEN 100 AND 599",
    )
    op.create_check_constraint(
        "webhook_deliveries_response_body_size_check",
        "webhook_deliveries",
        "last_response_body IS NULL OR length(last_response_body) <= 1024",
    )
    op.execute(
        """
        ALTER TABLE webhook_deliveries
          ADD CONSTRAINT webhook_deliveries_last_error_size_check
          CHECK (last_error IS NULL OR length(last_error) <= 100) NOT VALID
        """
    )
    op.create_check_constraint(
        "webhook_deliveries_lease_check",
        "webhook_deliveries",
        "(lease_token IS NULL) = (lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "webhook_deliveries_terminal_state_check",
        "webhook_deliveries",
        """
        (status IN ('pending', 'retrying')
          AND delivered_at IS NULL
          AND failed_at IS NULL
          AND next_attempt_at IS NOT NULL)
        OR
        (status = 'delivered'
          AND delivered_at IS NOT NULL
          AND failed_at IS NULL
          AND next_attempt_at IS NULL)
        OR
        (status = 'failed'
          AND delivered_at IS NULL
          AND failed_at IS NOT NULL
          AND next_attempt_at IS NULL)
        """,
    )
    op.execute(
        """
        ALTER TABLE webhook_deliveries
          ADD CONSTRAINT webhook_deliveries_event_type_check
          CHECK (event_type IN (
            'submission.created',
            'verification.completed',
            'mcp_request.completed',
            'payment.succeeded',
            'payment.failed'
          )) NOT VALID
        """
    )
    op.drop_index("webhook_retry_idx", table_name="webhook_deliveries")
    op.create_index(
        "webhook_due_idx",
        "webhook_deliveries",
        ["next_attempt_at", "created_at"],
        postgresql_where=sa.text("status IN ('pending', 'retrying')"),
    )
    op.drop_constraint(
        "webhook_deliveries_endpoint_id_fkey",
        "webhook_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "webhook_deliveries_endpoint_id_fkey",
        "webhook_deliveries",
        "webhook_endpoints",
        ["endpoint_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _restore_webhook_baseline() -> None:
    # Older workers cannot decrypt the versioned endpoint envelope. Make the
    # downgrade fail closed before removing the ciphertext and lease columns.
    op.execute(
        """
        UPDATE webhook_deliveries
           SET status = 'failed'::delivery_status,
               next_attempt_at = NULL,
               delivered_at = NULL,
               failed_at = COALESCE(failed_at, now()),
               last_error = 'webhook_disabled_by_downgrade',
               lease_token = NULL,
               lease_expires_at = NULL
         WHERE status IN ('pending', 'retrying')
        """
    )
    op.execute(
        """
        UPDATE webhook_endpoints
           SET status = 'disabled'::integration_status
        """
    )
    op.drop_constraint(
        "webhook_deliveries_endpoint_id_fkey",
        "webhook_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "webhook_deliveries_endpoint_id_fkey",
        "webhook_deliveries",
        "webhook_endpoints",
        ["endpoint_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("webhook_due_idx", table_name="webhook_deliveries")
    op.create_index(
        "webhook_retry_idx",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending', 'retrying')"),
    )
    for name in (
        "webhook_deliveries_event_type_check",
        "webhook_deliveries_terminal_state_check",
        "webhook_deliveries_lease_check",
        "webhook_deliveries_last_error_size_check",
        "webhook_deliveries_response_body_size_check",
        "webhook_deliveries_response_code_check",
        "webhook_deliveries_payload_body_size_check",
        "webhook_deliveries_deduplication_key_check",
    ):
        op.drop_constraint(name, "webhook_deliveries", type_="check")
    op.drop_constraint(
        "webhook_deliveries_endpoint_id_deduplication_key_key",
        "webhook_deliveries",
        type_="unique",
    )
    for column in (
        "lease_expires_at",
        "lease_token",
        "failed_at",
        "last_response_body",
        "last_response_code",
        "last_attempt_at",
        "payload_body",
        "deduplication_key",
    ):
        op.drop_column("webhook_deliveries", column)

    op.execute("DROP TRIGGER webhook_endpoints_validate_creator ON webhook_endpoints")
    op.drop_index("webhook_endpoints_creator_active_key", table_name="webhook_endpoints")
    for name in (
        "webhook_endpoints_subscribed_events_check",
        "webhook_endpoints_encrypted_url_check",
        "webhook_endpoints_active_secret_check",
    ):
        op.drop_constraint(name, "webhook_endpoints", type_="check")
    op.execute("DROP FUNCTION hah_text_array_is_unique(text[])")
    op.drop_column("webhook_endpoints", "secret_ciphertext")


def _restore_claimed_bounty_guard() -> None:
    op.execute("DROP TRIGGER bounties_claimed_pricing_immutable ON bounties")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_claimed_bounty_pricing_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (NEW.task_id IS DISTINCT FROM OLD.task_id
              OR NEW.reward_minor IS DISTINCT FROM OLD.reward_minor)
             AND EXISTS (SELECT 1 FROM bounty_claims WHERE bounty_id = OLD.id) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'claimed bounty pricing and task cannot be changed';
          END IF;
          IF NEW.status = 'cancelled'
             AND NEW.status IS DISTINCT FROM OLD.status
             AND EXISTS (
               SELECT 1
                 FROM bounty_claims
                WHERE bounty_id = OLD.id
                  AND hah_claim_occupies_slot(status, claim_expires_at)
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'bounty with active claims cannot be cancelled';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER bounties_claimed_pricing_immutable
        BEFORE UPDATE OF task_id, reward_minor, status ON bounties
        FOR EACH ROW EXECUTE FUNCTION prevent_claimed_bounty_pricing_change()
        """
    )


def _restore_submission_state_machine() -> None:
    op.execute("DROP TRIGGER submission_proofs_guard_changes ON submission_proofs")
    op.execute("DROP FUNCTION hah_guard_submission_proofs()")
    op.execute("DROP TRIGGER submissions_guard_verification ON submissions")
    op.execute("DROP FUNCTION hah_guard_submission_verification()")
    op.execute("DROP TRIGGER submissions_validate_insert ON submissions")
    op.execute("DROP FUNCTION hah_validate_submission_insert()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION apply_submission_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE bounty_claims
             SET status = 'submitted', submitted_at = NEW.submitted_at
           WHERE id = NEW.claim_id
             AND status IN ('claimed', 'changes_requested');
          IF NOT FOUND THEN
            RAISE EXCEPTION 'claim cannot accept a submission in its current state';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION apply_verification_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.verification_status = 'passed' THEN
            IF EXISTS (
              SELECT 1
                FROM bounty_claims c
                JOIN bounties b ON b.id = c.bounty_id
                CROSS JOIN LATERAL
                  jsonb_array_elements_text(b.proof_requirements) required(kind)
               WHERE c.id = NEW.claim_id
                 AND NOT EXISTS (
                   SELECT 1 FROM submission_proofs p
                    WHERE p.submission_id = NEW.id AND p.kind = required.kind
                 )
            ) THEN
              RAISE EXCEPTION 'submission is missing a required proof type';
            END IF;
            UPDATE bounty_claims
               SET status = 'approved', approved_at = NEW.verified_at
             WHERE id = NEW.claim_id AND status IN ('submitted', 'reviewing');
          ELSIF NEW.verification_status = 'failed' THEN
            UPDATE bounty_claims SET status = 'rejected'
             WHERE id = NEW.claim_id AND status IN ('submitted', 'reviewing');
          ELSIF NEW.verification_status = 'review_required' THEN
            UPDATE bounty_claims SET status = 'reviewing'
             WHERE id = NEW.claim_id AND status = 'submitted';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )


def _restore_submission_tables() -> None:
    for name in (
        "submission_proofs_sha256_check",
        "submission_proofs_storage_key_nonblank_check",
        "submission_proofs_https_url_check",
    ):
        op.drop_constraint(name, "submission_proofs", type_="check")
    op.drop_constraint(
        "submission_proofs_submission_id_kind_key",
        "submission_proofs",
        type_="unique",
    )
    op.drop_constraint("submission_proofs_shape_check", "submission_proofs", type_="check")
    op.create_check_constraint(
        "submission_proofs_check",
        "submission_proofs",
        """
        (kind = 'url' AND external_url IS NOT NULL AND storage_key IS NULL)
        OR
        (kind IN ('screenshot', 'image') AND storage_key IS NOT NULL)
        """,
    )
    op.drop_constraint(
        "submissions_verification_state_check",
        "submissions",
        type_="check",
    )
    op.create_check_constraint(
        "submissions_check",
        "submissions",
        """
        verification_status IN ('pending', 'review_required') OR
        (verification_method IS NOT NULL AND verified_at IS NOT NULL)
        """,
    )


def _drop_mcp_submission_link() -> None:
    op.drop_index("mcp_requests_submission_id_idx", table_name="mcp_requests")
    op.drop_constraint("mcp_requests_submission_id_fkey", "mcp_requests", type_="foreignkey")
    op.drop_column("mcp_requests", "submission_id")
