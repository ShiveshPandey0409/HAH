"""Add temporary image proof uploads and secure proof binding.

Revision ID: 20260802_0008
Revises: 20260802_0007
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0008"
down_revision: str | None = "20260802_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    op.create_table(
        "proof_uploads",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "claim_id",
            sa.Uuid(),
            sa.ForeignKey("bounty_claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "freelancer_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('screenshot', 'image')",
            name="proof_uploads_kind_check",
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')",
            name="proof_uploads_mime_type_check",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 5242880",
            name="proof_uploads_size_check",
        ),
        sa.CheckConstraint(
            "octet_length(content) = size_bytes",
            name="proof_uploads_content_size_check",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="proof_uploads_sha256_check",
        ),
        comment="Temporary hackathon image storage tied to one claimed bounty.",
    )
    op.create_index("proof_uploads_claim_id_idx", "proof_uploads", ["claim_id"])
    op.create_index("proof_uploads_freelancer_id_idx", "proof_uploads", ["freelancer_id"])

    op.add_column("submission_proofs", sa.Column("upload_id", sa.Uuid()))
    op.create_foreign_key(
        "submission_proofs_upload_id_fkey",
        "submission_proofs",
        "proof_uploads",
        ["upload_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "submission_proofs_upload_id_key",
        "submission_proofs",
        ["upload_id"],
    )
    op.drop_constraint("submission_proofs_shape_check", "submission_proofs", type_="check")
    op.execute(
        """
        ALTER TABLE submission_proofs
          ADD CONSTRAINT submission_proofs_shape_check CHECK (
            (kind = 'url'
              AND external_url IS NOT NULL
              AND storage_key IS NULL
              AND mime_type IS NULL
              AND sha256 IS NULL
              AND upload_id IS NULL)
            OR
            (kind IN ('screenshot', 'image')
              AND external_url IS NULL
              AND storage_key IS NOT NULL
              AND upload_id IS NOT NULL)
          ) NOT VALID
        """
    )

    op.execute(
        """
        CREATE FUNCTION hah_validate_proof_upload()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_claim bounty_claims%ROWTYPE;
          v_requirements jsonb;
        BEGIN
          SELECT * INTO v_claim
            FROM bounty_claims
           WHERE id = NEW.claim_id
           FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'claim not found';
          END IF;
          IF v_claim.freelancer_id <> NEW.freelancer_id THEN
            RAISE EXCEPTION USING ERRCODE = 'HVL01', MESSAGE = 'upload owner is invalid';
          END IF;
          IF v_claim.status NOT IN ('claimed', 'changes_requested') THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'claim cannot accept an upload';
          END IF;
          IF v_claim.status = 'claimed'
             AND v_claim.claim_expires_at IS NOT NULL
             AND v_claim.claim_expires_at <= now() THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'claim reservation has expired';
          END IF;
          SELECT proof_requirements INTO v_requirements
            FROM bounties
           WHERE id = v_claim.bounty_id;
          IF v_requirements IS NULL OR NOT v_requirements ? NEW.kind THEN
            RAISE EXCEPTION USING ERRCODE = 'HVL01', MESSAGE = 'proof type is not required';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER proof_uploads_validate_insert
        BEFORE INSERT ON proof_uploads
        FOR EACH ROW EXECUTE FUNCTION hah_validate_proof_upload()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_proof_upload_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'proof upload is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER proof_uploads_immutable
        BEFORE UPDATE ON proof_uploads
        FOR EACH ROW EXECUTE FUNCTION hah_guard_proof_upload_update()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION hah_guard_submission_proofs()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_submission_id uuid;
          v_claim_id uuid;
          v_freelancer_id uuid;
          v_status verification_status;
        BEGIN
          IF TG_OP = 'UPDATE'
             AND (NEW.submission_id IS DISTINCT FROM OLD.submission_id
                  OR NEW.upload_id IS DISTINCT FROM OLD.upload_id) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'proof submission ownership is immutable';
          END IF;
          v_submission_id := CASE
            WHEN TG_OP = 'DELETE' THEN OLD.submission_id ELSE NEW.submission_id
          END;
          SELECT s.verification_status, s.claim_id, c.freelancer_id
            INTO v_status, v_claim_id, v_freelancer_id
            FROM submissions s
            JOIN bounty_claims c ON c.id = s.claim_id
           WHERE s.id = v_submission_id
           FOR UPDATE OF s;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'submission not found';
          END IF;
          IF v_status <> 'pending' THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HCF01', MESSAGE = 'proofs are immutable after review begins';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;

          IF NEW.kind IN ('screenshot', 'image') AND NOT EXISTS (
            SELECT 1
              FROM proof_uploads upload
             WHERE upload.id = NEW.upload_id
               AND upload.claim_id = v_claim_id
               AND upload.freelancer_id = v_freelancer_id
               AND upload.kind = NEW.kind
               AND upload.mime_type = NEW.mime_type
               AND upload.sha256 = NEW.sha256
               AND NEW.storage_key = 'proof-uploads/' || upload.id::text
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'HVL01', MESSAGE = 'proof upload binding is invalid';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_validate_uploaded_proofs_before_verification()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.verification_status = 'passed'
             AND NEW.verification_status IS DISTINCT FROM OLD.verification_status
             AND EXISTS (
               SELECT 1
                 FROM submission_proofs proof
            LEFT JOIN proof_uploads upload ON upload.id = proof.upload_id
                WHERE proof.submission_id = NEW.id
                  AND proof.kind IN ('screenshot', 'image')
                  AND (
                    upload.id IS NULL
                    OR upload.claim_id <> NEW.claim_id
                    OR upload.kind <> proof.kind
                    OR upload.mime_type <> proof.mime_type
                    OR upload.sha256 <> proof.sha256
                  )
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'submission contains an invalid upload';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER submissions_validate_uploaded_proofs
        BEFORE UPDATE OF verification_status ON submissions
        FOR EACH ROW EXECUTE FUNCTION hah_validate_uploaded_proofs_before_verification()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER submissions_validate_uploaded_proofs ON submissions")
    op.execute("DROP FUNCTION hah_validate_uploaded_proofs_before_verification()")
    op.execute("DROP TRIGGER proof_uploads_immutable ON proof_uploads")
    op.execute("DROP FUNCTION hah_guard_proof_upload_update()")
    op.execute("DROP TRIGGER proof_uploads_validate_insert ON proof_uploads")
    op.execute("DROP FUNCTION hah_validate_proof_upload()")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION hah_guard_submission_proofs()
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

    op.drop_constraint("submission_proofs_shape_check", "submission_proofs", type_="check")
    op.create_check_constraint(
        "submission_proofs_shape_check",
        "submission_proofs",
        """
        (kind = 'url' AND external_url IS NOT NULL AND storage_key IS NULL
          AND mime_type IS NULL AND sha256 IS NULL)
        OR
        (kind IN ('screenshot', 'image') AND external_url IS NULL
          AND storage_key IS NOT NULL)
        """,
    )
    op.drop_constraint("submission_proofs_upload_id_key", "submission_proofs", type_="unique")
    op.drop_constraint("submission_proofs_upload_id_fkey", "submission_proofs", type_="foreignkey")
    op.drop_column("submission_proofs", "upload_id")
    op.drop_index("proof_uploads_freelancer_id_idx", table_name="proof_uploads")
    op.drop_index("proof_uploads_claim_id_idx", table_name="proof_uploads")
    op.drop_table("proof_uploads")
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
          'mcp:access', 'tasks:create', 'submissions:verify', 'submissions:approve'
        ]::text[]
        AND approved_scopes @> ARRAY['mcp:access']::text[]
        """,
    )
