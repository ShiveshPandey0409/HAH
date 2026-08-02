"""Add the first-party OAuth authorization server store.

Revision ID: 20260802_0010
Revises: 20260802_0009
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260802_0010"
down_revision: str | None = "20260802_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_registered_clients",
        sa.Column("client_id", sa.Text(), primary_key=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("client_secret_hash", sa.Text()),
        sa.Column("client_secret_ciphertext", sa.LargeBinary()),
        sa.Column("client_id_issued_at", sa.BigInteger(), nullable=False),
        sa.Column("client_secret_expires_at", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "client_id = btrim(client_id) AND client_id <> ''",
            name="oauth_registered_clients_client_id_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="oauth_registered_clients_metadata_check",
        ),
        sa.CheckConstraint(
            "(client_secret_hash IS NULL) = (client_secret_ciphertext IS NULL)",
            name="oauth_registered_clients_secret_pair_check",
        ),
        comment="Dynamically registered MCP OAuth clients; secrets are encrypted.",
    )

    op.create_table(
        "oauth_authorization_requests",
        sa.Column("request_hash", sa.Text(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Text(),
            sa.ForeignKey("oauth_registered_clients.client_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.Text()),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="oauth_authorization_requests_hash_check",
        ),
        sa.CheckConstraint(
            "array_position(scopes, NULL) IS NULL AND hah_text_array_is_unique(scopes)",
            name="oauth_authorization_requests_scopes_check",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at <= expires_at",
            name="oauth_authorization_requests_consumed_check",
        ),
        comment="Short-lived browser consent requests; raw handles are never stored.",
    )
    op.create_index(
        "oauth_authorization_requests_expires_at_idx",
        "oauth_authorization_requests",
        ["expires_at"],
    )

    op.create_table(
        "oauth_authorization_codes",
        sa.Column("code_hash", sa.Text(), primary_key=True),
        sa.Column(
            "delegation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oauth_delegations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Text(),
            sa.ForeignKey("oauth_registered_clients.client_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authorization_id", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "code_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="oauth_authorization_codes_hash_check",
        ),
        sa.CheckConstraint(
            "array_position(scopes, NULL) IS NULL AND hah_text_array_is_unique(scopes)",
            name="oauth_authorization_codes_scopes_check",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at <= expires_at",
            name="oauth_authorization_codes_consumed_check",
        ),
        comment="One-time PKCE authorization codes; only SHA-256 hashes are stored.",
    )
    op.create_index(
        "oauth_authorization_codes_expires_at_idx",
        "oauth_authorization_codes",
        ["expires_at"],
    )

    op.create_table(
        "oauth_issued_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_kind", sa.Text(), nullable=False),
        sa.Column(
            "delegation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oauth_delegations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Text(),
            sa.ForeignKey("oauth_registered_clients.client_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authorization_id", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="oauth_issued_tokens_token_hash_key"),
        sa.CheckConstraint(
            "token_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="oauth_issued_tokens_hash_check",
        ),
        sa.CheckConstraint(
            "token_kind IN ('access', 'refresh')",
            name="oauth_issued_tokens_kind_check",
        ),
        sa.CheckConstraint(
            "array_position(scopes, NULL) IS NULL AND hah_text_array_is_unique(scopes)",
            name="oauth_issued_tokens_scopes_check",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at <= expires_at",
            name="oauth_issued_tokens_revoked_check",
        ),
        comment="Hashed OAuth access and rotating refresh tokens.",
    )
    op.create_index(
        "oauth_issued_tokens_family_id_idx",
        "oauth_issued_tokens",
        ["family_id"],
    )
    op.create_index(
        "oauth_issued_tokens_expires_at_idx",
        "oauth_issued_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM oauth_registered_clients)
             OR EXISTS (SELECT 1 FROM oauth_authorization_requests)
             OR EXISTS (SELECT 1 FROM oauth_authorization_codes)
             OR EXISTS (SELECT 1 FROM oauth_issued_tokens) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HMG03',
              MESSAGE = 'first-party OAuth data must be removed before downgrade';
          END IF;
        END;
        $$
        """
    )
    op.drop_index("oauth_issued_tokens_expires_at_idx", table_name="oauth_issued_tokens")
    op.drop_index("oauth_issued_tokens_family_id_idx", table_name="oauth_issued_tokens")
    op.drop_table("oauth_issued_tokens")
    op.drop_index(
        "oauth_authorization_codes_expires_at_idx",
        table_name="oauth_authorization_codes",
    )
    op.drop_table("oauth_authorization_codes")
    op.drop_index(
        "oauth_authorization_requests_expires_at_idx",
        table_name="oauth_authorization_requests",
    )
    op.drop_table("oauth_authorization_requests")
    op.drop_table("oauth_registered_clients")
