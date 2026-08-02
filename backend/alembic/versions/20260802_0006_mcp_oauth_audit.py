"""Add user-delegated OAuth identities and MCP audit ownership.

Revision ID: 20260802_0006
Revises: 20260802_0005
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260802_0006"
down_revision: str | None = "20260802_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_oauth_tables()
    _install_oauth_mutation_guards()
    _extend_mcp_request_audit()
    _install_mcp_actor_guard()


def downgrade() -> None:
    _refuse_lossy_oauth_downgrade()
    _remove_mcp_actor_guard()
    _restore_legacy_mcp_request_audit()
    _remove_oauth_mutation_guards()
    op.drop_table("oauth_authorization_grants")
    op.drop_table("oauth_delegations")
    op.drop_table("oauth_identities")


def _create_oauth_tables() -> None:
    integration_status = postgresql.ENUM(
        "active",
        "disabled",
        name="integration_status",
        create_type=False,
    )
    op.create_table(
        "oauth_identities",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "status",
            integration_status,
            nullable=False,
            server_default=sa.text("'active'::integration_status"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "issuer = btrim(issuer) AND issuer <> ''",
            name="oauth_identities_issuer_check",
        ),
        sa.CheckConstraint(
            "subject = btrim(subject) AND subject <> ''",
            name="oauth_identities_subject_check",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="oauth_identities_user_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "issuer",
            "subject",
            name="oauth_identities_issuer_subject_key",
        ),
        comment="Exact external OAuth issuer/subject identity mapped to one user.",
    )
    op.create_index(
        "oauth_identities_user_id_idx",
        "oauth_identities",
        ["user_id"],
    )

    op.create_table(
        "oauth_delegations",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("oauth_client_id", sa.Text(), nullable=False),
        sa.Column("approved_scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "status",
            integration_status,
            nullable=False,
            server_default=sa.text("'active'::integration_status"),
        ),
        sa.Column(
            "consent_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "consented_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("authorization_id", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "oauth_client_id = btrim(oauth_client_id) AND oauth_client_id <> ''",
            name="oauth_delegations_oauth_client_id_check",
        ),
        sa.CheckConstraint(
            "authorization_id = btrim(authorization_id) AND authorization_id <> '' "
            "AND char_length(authorization_id) <= 2048",
            name="oauth_delegations_authorization_id_check",
        ),
        sa.CheckConstraint(
            "array_position(approved_scopes, NULL) IS NULL "
            "AND hah_text_array_is_unique(approved_scopes)",
            name="oauth_delegations_approved_scopes_check",
        ),
        sa.CheckConstraint(
            "approved_scopes <@ ARRAY["
            "'mcp:access', 'tasks:create', 'submissions:verify', 'submissions:approve'"
            "]::text[] AND approved_scopes @> ARRAY['mcp:access']::text[]",
            name="oauth_delegations_supported_scopes_check",
        ),
        sa.CheckConstraint(
            "consent_version > 0",
            name="oauth_delegations_consent_version_check",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= consented_at",
            name="oauth_delegations_revoked_at_check",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR revoked_at IS NULL",
            name="oauth_delegations_active_not_revoked_check",
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["oauth_identities.id"],
            name="oauth_delegations_identity_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "identity_id",
            "oauth_client_id",
            name="oauth_delegations_identity_id_oauth_client_id_key",
        ),
        comment="Per-user approval for one external OAuth client and scope set.",
    )
    op.create_table(
        "oauth_authorization_grants",
        sa.Column("delegation_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "authorization_id = btrim(authorization_id) AND authorization_id <> '' "
            "AND char_length(authorization_id) <= 2048",
            name="oauth_authorization_grants_authorization_id_check",
        ),
        sa.ForeignKeyConstraint(
            ["delegation_id"],
            ["oauth_delegations.id"],
            name="oauth_authorization_grants_delegation_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "delegation_id",
            "authorization_id",
            name="oauth_authorization_grants_pkey",
        ),
        comment="Immutable history of authorization-server grant handles.",
    )


def _extend_mcp_request_audit() -> None:
    op.add_column(
        "mcp_requests",
        sa.Column("oauth_delegation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "mcp_requests",
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "mcp_requests",
        sa.Column("auth_scopes", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "mcp_requests",
        sa.Column("oauth_consent_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mcp_requests",
        sa.Column("oauth_authorization_id", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "mcp_requests_oauth_delegation_id_fkey",
        "mcp_requests",
        "oauth_delegations",
        ["oauth_delegation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "mcp_requests_actor_user_id_fkey",
        "mcp_requests",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        UPDATE mcp_requests AS request
           SET actor_user_id = client.creator_id,
               auth_scopes = ARRAY(
                 SELECT DISTINCT scope
                   FROM unnest(client.scopes) AS allowed(scope)
                  WHERE scope IS NOT NULL
                  ORDER BY scope
               )
          FROM api_clients AS client
         WHERE client.id = request.api_client_id
        """
    )
    op.alter_column(
        "mcp_requests",
        "api_client_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.alter_column(
        "mcp_requests",
        "actor_user_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.alter_column(
        "mcp_requests",
        "auth_scopes",
        existing_type=postgresql.ARRAY(sa.Text()),
        nullable=False,
    )
    op.create_check_constraint(
        "mcp_requests_auth_source_check",
        "mcp_requests",
        "num_nonnulls(api_client_id, oauth_delegation_id) = 1",
    )
    op.create_check_constraint(
        "mcp_requests_oauth_consent_version_check",
        "mcp_requests",
        "(api_client_id IS NOT NULL AND oauth_consent_version IS NULL) OR "
        "(oauth_delegation_id IS NOT NULL AND oauth_consent_version > 0)",
    )
    op.create_check_constraint(
        "mcp_requests_oauth_authorization_id_check",
        "mcp_requests",
        "(api_client_id IS NOT NULL AND oauth_authorization_id IS NULL) OR "
        "(oauth_delegation_id IS NOT NULL "
        "AND oauth_authorization_id = btrim(oauth_authorization_id) "
        "AND oauth_authorization_id <> '')",
    )
    op.create_check_constraint(
        "mcp_requests_auth_scopes_check",
        "mcp_requests",
        "array_position(auth_scopes, NULL) IS NULL AND hah_text_array_is_unique(auth_scopes)",
    )
    op.create_index(
        "mcp_requests_oauth_delegation_id_idempotency_key_key",
        "mcp_requests",
        ["oauth_delegation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("oauth_delegation_id IS NOT NULL"),
    )


def _install_oauth_mutation_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_guard_oauth_identity_mapping()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.user_id IS DISTINCT FROM OLD.user_id
             OR NEW.issuer IS DISTINCT FROM OLD.issuer
             OR NEW.subject IS DISTINCT FROM OLD.subject THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01',
              MESSAGE = 'OAuth identity mappings are immutable';
          END IF;
          IF OLD.status = 'disabled' AND NEW.status = 'active' THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01',
              MESSAGE = 'disabled OAuth identities cannot be reactivated';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER oauth_identities_guard_mapping
        BEFORE UPDATE OF user_id, issuer, subject, status ON oauth_identities
        FOR EACH ROW EXECUTE FUNCTION hah_guard_oauth_identity_mapping()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_oauth_delegation_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_authorization_changed boolean;
          v_reconsented boolean;
        BEGIN
          IF NEW.identity_id IS DISTINCT FROM OLD.identity_id
             OR NEW.oauth_client_id IS DISTINCT FROM OLD.oauth_client_id THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01',
              MESSAGE = 'OAuth delegation identity and client are immutable';
          END IF;

          IF NEW.authorization_id IS DISTINCT FROM OLD.authorization_id
             AND EXISTS (
               SELECT 1
                 FROM oauth_authorization_grants AS grant_history
                WHERE grant_history.delegation_id = OLD.id
                  AND grant_history.authorization_id = NEW.authorization_id
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01',
              MESSAGE = 'OAuth authorization grant handles cannot be reused';
          END IF;

          v_authorization_changed :=
            NEW.approved_scopes IS DISTINCT FROM OLD.approved_scopes
            OR NEW.status IS DISTINCT FROM OLD.status
            OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
            OR NEW.authorization_id IS DISTINCT FROM OLD.authorization_id;
          v_reconsented :=
            NEW.approved_scopes IS DISTINCT FROM OLD.approved_scopes
            OR NEW.authorization_id IS DISTINCT FROM OLD.authorization_id
            OR (
              NEW.status = 'active'
              AND NEW.revoked_at IS NULL
              AND (OLD.status <> 'active' OR OLD.revoked_at IS NOT NULL)
            );

          IF v_authorization_changed THEN
            IF NEW.consent_version <> OLD.consent_version + 1 THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01',
                MESSAGE = 'OAuth authorization changes must rotate consent version';
            END IF;
            IF v_reconsented AND NEW.consented_at <= OLD.consented_at THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01',
                MESSAGE = 'OAuth approval changes require fresh consent';
            END IF;
            IF v_reconsented
               AND NEW.authorization_id IS NOT DISTINCT FROM OLD.authorization_id THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01',
                MESSAGE = 'OAuth approval changes require a fresh authorization grant';
            END IF;
          ELSIF NEW.consent_version IS DISTINCT FROM OLD.consent_version
                OR NEW.consented_at IS DISTINCT FROM OLD.consented_at
                OR NEW.authorization_id IS DISTINCT FROM OLD.authorization_id THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01',
              MESSAGE = 'OAuth consent cannot change without authorization changes';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER oauth_delegations_guard_mutation
        BEFORE UPDATE OF
          identity_id, oauth_client_id, approved_scopes, status,
          consent_version, consented_at, authorization_id, revoked_at
        ON oauth_delegations
        FOR EACH ROW EXECUTE FUNCTION hah_guard_oauth_delegation_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_record_oauth_authorization_grant()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            INSERT INTO oauth_authorization_grants (delegation_id, authorization_id)
            VALUES (NEW.id, NEW.authorization_id);
          ELSIF NEW.authorization_id IS DISTINCT FROM OLD.authorization_id THEN
            INSERT INTO oauth_authorization_grants (delegation_id, authorization_id)
            VALUES (NEW.id, NEW.authorization_id);
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER oauth_delegations_record_authorization_grant
        AFTER INSERT OR UPDATE OF authorization_id ON oauth_delegations
        FOR EACH ROW EXECUTE FUNCTION hah_record_oauth_authorization_grant()
        """
    )
    op.execute(
        """
        CREATE FUNCTION hah_guard_oauth_authorization_grant_history()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION USING
            ERRCODE = 'HVL01',
            MESSAGE = 'OAuth authorization grant history is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER oauth_authorization_grants_immutable
        BEFORE UPDATE OR DELETE ON oauth_authorization_grants
        FOR EACH ROW EXECUTE FUNCTION hah_guard_oauth_authorization_grant_history()
        """
    )
    op.execute(
        """
        CREATE TRIGGER oauth_delegations_updated_at
        BEFORE UPDATE ON oauth_delegations
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def _install_mcp_actor_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION hah_validate_mcp_request_actor()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_expected_user_id uuid;
          v_allowed_scopes text[];
          v_consent_version integer;
          v_authorization_id text;
        BEGIN
          IF TG_OP = 'UPDATE' AND (
               NEW.api_client_id IS DISTINCT FROM OLD.api_client_id
               OR NEW.oauth_delegation_id IS DISTINCT FROM OLD.oauth_delegation_id
               OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
               OR NEW.auth_scopes IS DISTINCT FROM OLD.auth_scopes
               OR NEW.oauth_consent_version IS DISTINCT FROM OLD.oauth_consent_version
               OR NEW.oauth_authorization_id IS DISTINCT FROM OLD.oauth_authorization_id
               OR NEW.method IS DISTINCT FROM OLD.method
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.request_data IS DISTINCT FROM OLD.request_data
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'MCP authorization audit snapshot is immutable';
          END IF;

          IF num_nonnulls(NEW.api_client_id, NEW.oauth_delegation_id) <> 1 THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'exactly one MCP authorization source is required';
          END IF;

          IF NEW.api_client_id IS NOT NULL THEN
            SELECT client.creator_id,
                   ARRAY(
                     SELECT DISTINCT scope
                       FROM unnest(client.scopes) AS allowed(scope)
                      WHERE scope IS NOT NULL
                      ORDER BY scope
                   )
              INTO v_expected_user_id, v_allowed_scopes
              FROM api_clients AS client
             WHERE client.id = NEW.api_client_id
               AND client.status = 'active';
            IF NOT FOUND THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HAU01', MESSAGE = 'API client is not active';
            END IF;
            IF NEW.auth_scopes IS NULL THEN
              NEW.auth_scopes := v_allowed_scopes;
            END IF;
            IF NEW.oauth_consent_version IS NOT NULL THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01', MESSAGE = 'legacy API requests cannot name OAuth consent';
            END IF;
            IF NEW.oauth_authorization_id IS NOT NULL THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01', MESSAGE = 'legacy API requests cannot name OAuth authorization';
            END IF;
          ELSE
            SELECT identity.user_id,
                   delegation.approved_scopes,
                   delegation.consent_version,
                   delegation.authorization_id
              INTO v_expected_user_id, v_allowed_scopes, v_consent_version,
                   v_authorization_id
              FROM oauth_delegations AS delegation
              JOIN oauth_identities AS identity ON identity.id = delegation.identity_id
             WHERE delegation.id = NEW.oauth_delegation_id
               AND delegation.status = 'active'
               AND delegation.revoked_at IS NULL
               AND identity.status = 'active';
            IF NOT FOUND THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HAU01', MESSAGE = 'OAuth delegation is not active';
            END IF;
            IF NEW.auth_scopes IS NULL THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HVL01', MESSAGE = 'OAuth scope snapshot is required';
            END IF;
            IF NEW.oauth_consent_version IS DISTINCT FROM v_consent_version THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HAU01', MESSAGE = 'OAuth consent version is stale';
            END IF;
            IF NEW.oauth_authorization_id IS NULL THEN
              NEW.oauth_authorization_id := v_authorization_id;
            ELSIF NEW.oauth_authorization_id IS DISTINCT FROM v_authorization_id THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HAU01', MESSAGE = 'OAuth authorization grant is stale';
            END IF;
            IF NOT NEW.auth_scopes @> ARRAY['mcp:access']::text[] THEN
              RAISE EXCEPTION USING
                ERRCODE = 'HAU01', MESSAGE = 'OAuth MCP access scope is required';
            END IF;
            IF NEW.method = 'create_task' THEN
              IF NOT NEW.auth_scopes @> ARRAY['tasks:create']::text[] THEN
                RAISE EXCEPTION USING
                  ERRCODE = 'HAU01', MESSAGE = 'OAuth task creation scope is required';
              END IF;
            ELSIF NEW.method = 'verify_submission' THEN
              IF NOT NEW.auth_scopes @> ARRAY['submissions:verify']::text[] THEN
                RAISE EXCEPTION USING
                  ERRCODE = 'HAU01', MESSAGE = 'OAuth verification scope is required';
              END IF;
              IF NEW.request_data ->> 'result' = 'passed'
                 AND NOT NEW.auth_scopes @> ARRAY['submissions:approve']::text[] THEN
                RAISE EXCEPTION USING
                  ERRCODE = 'HAU01', MESSAGE = 'OAuth approval scope is required';
              END IF;
            ELSE
              RAISE EXCEPTION USING
                ERRCODE = 'HAU01', MESSAGE = 'OAuth MCP method is not supported';
            END IF;
          END IF;

          IF NEW.actor_user_id IS NULL THEN
            NEW.actor_user_id := v_expected_user_id;
          ELSIF NEW.actor_user_id IS DISTINCT FROM v_expected_user_id THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'MCP actor does not own authorization source';
          END IF;
          IF NOT NEW.auth_scopes <@ v_allowed_scopes THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HAU01', MESSAGE = 'MCP request scope was not approved';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER mcp_requests_validate_actor
        BEFORE INSERT OR UPDATE OF
          api_client_id, oauth_delegation_id, actor_user_id,
          auth_scopes, oauth_consent_version, oauth_authorization_id,
          method, idempotency_key, request_data, started_at
        ON mcp_requests
        FOR EACH ROW EXECUTE FUNCTION hah_validate_mcp_request_actor()
        """
    )


def _refuse_lossy_oauth_downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM oauth_identities)
             OR EXISTS (SELECT 1 FROM oauth_delegations)
             OR EXISTS (
               SELECT 1 FROM mcp_requests WHERE oauth_delegation_id IS NOT NULL
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HMG01',
              MESSAGE = 'OAuth audit rows must be retained; refusing lossy downgrade';
          END IF;
        END;
        $$
        """
    )


def _remove_mcp_actor_guard() -> None:
    op.execute("DROP TRIGGER IF EXISTS mcp_requests_validate_actor ON mcp_requests")
    op.execute("DROP FUNCTION IF EXISTS hah_validate_mcp_request_actor()")


def _remove_oauth_mutation_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS oauth_authorization_grants_immutable ON oauth_authorization_grants"
    )
    op.execute("DROP FUNCTION IF EXISTS hah_guard_oauth_authorization_grant_history()")
    op.execute(
        "DROP TRIGGER IF EXISTS oauth_delegations_record_authorization_grant ON oauth_delegations"
    )
    op.execute("DROP FUNCTION IF EXISTS hah_record_oauth_authorization_grant()")
    op.execute("DROP TRIGGER IF EXISTS oauth_delegations_updated_at ON oauth_delegations")
    op.execute("DROP TRIGGER IF EXISTS oauth_delegations_guard_mutation ON oauth_delegations")
    op.execute("DROP FUNCTION IF EXISTS hah_guard_oauth_delegation_mutation()")
    op.execute("DROP TRIGGER IF EXISTS oauth_identities_guard_mapping ON oauth_identities")
    op.execute("DROP FUNCTION IF EXISTS hah_guard_oauth_identity_mapping()")


def _restore_legacy_mcp_request_audit() -> None:
    op.drop_index(
        "mcp_requests_oauth_delegation_id_idempotency_key_key",
        table_name="mcp_requests",
    )
    op.drop_constraint(
        "mcp_requests_auth_scopes_check",
        "mcp_requests",
        type_="check",
    )
    op.drop_constraint(
        "mcp_requests_oauth_consent_version_check",
        "mcp_requests",
        type_="check",
    )
    op.drop_constraint(
        "mcp_requests_oauth_authorization_id_check",
        "mcp_requests",
        type_="check",
    )
    op.drop_constraint(
        "mcp_requests_auth_source_check",
        "mcp_requests",
        type_="check",
    )
    op.alter_column(
        "mcp_requests",
        "api_client_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.drop_constraint(
        "mcp_requests_actor_user_id_fkey",
        "mcp_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "mcp_requests_oauth_delegation_id_fkey",
        "mcp_requests",
        type_="foreignkey",
    )
    op.drop_column("mcp_requests", "oauth_authorization_id")
    op.drop_column("mcp_requests", "oauth_consent_version")
    op.drop_column("mcp_requests", "auth_scopes")
    op.drop_column("mcp_requests", "actor_user_id")
    op.drop_column("mcp_requests", "oauth_delegation_id")
