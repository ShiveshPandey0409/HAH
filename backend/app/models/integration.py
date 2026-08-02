from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IntegrationStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class RequestStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class APIClient(Base):
    __tablename__ = "api_clients"
    __table_args__ = ({"comment": "MCP/API credentials owned by a creator."},)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    creator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY[]::text[]"),
    )
    status: Mapped[IntegrationStatus] = mapped_column(
        ENUM(
            IntegrationStatus,
            name="integration_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'active'::integration_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (
        UniqueConstraint(
            "issuer",
            "subject",
            name="oauth_identities_issuer_subject_key",
        ),
        CheckConstraint(
            "issuer = btrim(issuer) AND issuer <> ''",
            name="oauth_identities_issuer_check",
        ),
        CheckConstraint(
            "subject = btrim(subject) AND subject <> ''",
            name="oauth_identities_subject_check",
        ),
        Index("oauth_identities_user_id_idx", "user_id"),
        {"comment": "Exact external OAuth issuer/subject identity mapped to one user."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[IntegrationStatus] = mapped_column(
        ENUM(
            IntegrationStatus,
            name="integration_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'active'::integration_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthDelegation(Base):
    __tablename__ = "oauth_delegations"
    __table_args__ = (
        UniqueConstraint(
            "identity_id",
            "oauth_client_id",
            name="oauth_delegations_identity_id_oauth_client_id_key",
        ),
        CheckConstraint(
            "oauth_client_id = btrim(oauth_client_id) AND oauth_client_id <> ''",
            name="oauth_delegations_oauth_client_id_check",
        ),
        CheckConstraint(
            "authorization_id = btrim(authorization_id) AND authorization_id <> '' "
            "AND char_length(authorization_id) <= 2048",
            name="oauth_delegations_authorization_id_check",
        ),
        CheckConstraint(
            "array_position(approved_scopes, NULL) IS NULL "
            "AND hah_text_array_is_unique(approved_scopes)",
            name="oauth_delegations_approved_scopes_check",
        ),
        CheckConstraint(
            "approved_scopes <@ ARRAY["
            "'mcp:access', 'tasks:create', 'submissions:read', "
            "'submissions:verify', 'submissions:approve', "
            "'payments:read', 'payments:write'"
            "]::text[] AND approved_scopes @> ARRAY['mcp:access']::text[]",
            name="oauth_delegations_supported_scopes_check",
        ),
        CheckConstraint(
            "consent_version > 0",
            name="oauth_delegations_consent_version_check",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= consented_at",
            name="oauth_delegations_revoked_at_check",
        ),
        CheckConstraint(
            "status <> 'active' OR revoked_at IS NULL",
            name="oauth_delegations_active_not_revoked_check",
        ),
        {"comment": "Per-user approval for one external OAuth client and scope set."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("oauth_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    oauth_client_id: Mapped[str] = mapped_column(Text, nullable=False)
    approved_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
    )
    status: Mapped[IntegrationStatus] = mapped_column(
        ENUM(
            IntegrationStatus,
            name="integration_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'active'::integration_status"),
    )
    consent_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    authorization_id: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthAuthorizationGrant(Base):
    __tablename__ = "oauth_authorization_grants"
    __table_args__ = (
        CheckConstraint(
            "authorization_id = btrim(authorization_id) AND authorization_id <> '' "
            "AND char_length(authorization_id) <= 2048",
            name="oauth_authorization_grants_authorization_id_check",
        ),
        {"comment": "Immutable history of authorization-server grant handles."},
    )

    delegation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("oauth_delegations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    authorization_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OAuthRegisteredClient(Base):
    __tablename__ = "oauth_registered_clients"
    __table_args__ = (
        CheckConstraint(
            "client_id = btrim(client_id) AND client_id <> ''",
            name="oauth_registered_clients_client_id_check",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="oauth_registered_clients_metadata_check",
        ),
        CheckConstraint(
            "(client_secret_hash IS NULL) = (client_secret_ciphertext IS NULL)",
            name="oauth_registered_clients_secret_pair_check",
        ),
        {"comment": "Dynamically registered MCP OAuth clients; secrets are encrypted."},
    )

    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    client_secret_hash: Mapped[str | None] = mapped_column(Text)
    client_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    client_id_issued_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_secret_expires_at: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OAuthAuthorizationRequest(Base):
    __tablename__ = "oauth_authorization_requests"
    __table_args__ = (
        CheckConstraint(
            "request_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="oauth_authorization_requests_hash_check",
        ),
        CheckConstraint(
            "array_position(scopes, NULL) IS NULL AND hah_text_array_is_unique(scopes)",
            name="oauth_authorization_requests_scopes_check",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at <= expires_at",
            name="oauth_authorization_requests_consumed_check",
        ),
        Index("oauth_authorization_requests_expires_at_idx", "expires_at"),
        {"comment": "Short-lived browser consent requests; raw handles are never stored."},
    )

    request_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_registered_clients.client_id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(Boolean, nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (
        CheckConstraint(
            "code_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="oauth_authorization_codes_hash_check",
        ),
        CheckConstraint(
            "array_position(scopes, NULL) IS NULL AND hah_text_array_is_unique(scopes)",
            name="oauth_authorization_codes_scopes_check",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at <= expires_at",
            name="oauth_authorization_codes_consumed_check",
        ),
        Index("oauth_authorization_codes_expires_at_idx", "expires_at"),
        {"comment": "One-time PKCE authorization codes; only SHA-256 hashes are stored."},
    )

    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    delegation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("oauth_delegations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_registered_clients.client_id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_id: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(Boolean, nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OAuthIssuedToken(Base):
    __tablename__ = "oauth_issued_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="oauth_issued_tokens_token_hash_key"),
        CheckConstraint(
            "token_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="oauth_issued_tokens_hash_check",
        ),
        CheckConstraint(
            "token_kind IN ('access', 'refresh')",
            name="oauth_issued_tokens_kind_check",
        ),
        CheckConstraint(
            "array_position(scopes, NULL) IS NULL AND hah_text_array_is_unique(scopes)",
            name="oauth_issued_tokens_scopes_check",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at <= expires_at",
            name="oauth_issued_tokens_revoked_check",
        ),
        Index("oauth_issued_tokens_family_id_idx", "family_id"),
        Index("oauth_issued_tokens_expires_at_idx", "expires_at"),
        {"comment": "Hashed OAuth access and rotating refresh tokens."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    token_kind: Mapped[str] = mapped_column(Text, nullable=False)
    delegation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("oauth_delegations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_registered_clients.client_id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_id: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    family_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MCPRequest(Base):
    __tablename__ = "mcp_requests"
    __table_args__ = (
        UniqueConstraint(
            "api_client_id",
            "idempotency_key",
            name="mcp_requests_api_client_id_idempotency_key_key",
        ),
        CheckConstraint(
            "num_nonnulls(api_client_id, oauth_delegation_id) = 1",
            name="mcp_requests_auth_source_check",
        ),
        CheckConstraint(
            "(api_client_id IS NOT NULL AND oauth_consent_version IS NULL) OR "
            "(oauth_delegation_id IS NOT NULL AND oauth_consent_version > 0)",
            name="mcp_requests_oauth_consent_version_check",
        ),
        CheckConstraint(
            "(api_client_id IS NOT NULL AND oauth_authorization_id IS NULL) OR "
            "(oauth_delegation_id IS NOT NULL "
            "AND oauth_authorization_id = btrim(oauth_authorization_id) "
            "AND oauth_authorization_id <> '')",
            name="mcp_requests_oauth_authorization_id_check",
        ),
        CheckConstraint(
            "array_position(auth_scopes, NULL) IS NULL AND hah_text_array_is_unique(auth_scopes)",
            name="mcp_requests_auth_scopes_check",
        ),
        Index(
            "mcp_requests_oauth_delegation_id_idempotency_key_key",
            "oauth_delegation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("oauth_delegation_id IS NOT NULL"),
        ),
        Index("mcp_requests_submission_id_idx", "submission_id"),
        {"comment": "Idempotent audit record for agent calls."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    api_client_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("api_clients.id", ondelete="RESTRICT"),
    )
    oauth_delegation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("oauth_delegations.id", ondelete="RESTRICT"),
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    auth_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    oauth_consent_version: Mapped[int | None] = mapped_column(Integer)
    oauth_authorization_id: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RequestStatus] = mapped_column(
        ENUM(
            RequestStatus,
            name="request_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'started'::request_status"),
    )
    request_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    response_data: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT"),
    )
    submission_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="RESTRICT"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
