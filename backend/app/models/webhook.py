from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
from app.models.integration import IntegrationStatus


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    RETRYING = "retrying"
    DELIVERED = "delivered"
    FAILED = "failed"


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


_SUPPORTED_EVENT_ARRAY = (
    "ARRAY['submission.created','verification.completed','mcp_request.completed',"
    "'payment.succeeded','payment.failed']::text[]"
)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        CheckConstraint(
            "url = 'https://encrypted.invalid/'",
            name="webhook_endpoints_encrypted_url_check",
        ),
        CheckConstraint(
            "status <> 'active' OR secret_ciphertext IS NOT NULL",
            name="webhook_endpoints_active_secret_check",
        ),
        CheckConstraint(
            f"subscribed_events <@ {_SUPPORTED_EVENT_ARRAY} "
            "AND array_position(subscribed_events, NULL) IS NULL "
            "AND hah_text_array_is_unique(subscribed_events)",
            name="webhook_endpoints_subscribed_events_check",
        ),
        Index(
            "webhook_endpoints_creator_active_key",
            "creator_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        {"comment": "Creator webhook configuration."},
    )

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
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    subscribed_events: Mapped[list[str]] = mapped_column(
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id",
            "event_id",
            name="webhook_deliveries_endpoint_id_event_id_key",
        ),
        UniqueConstraint(
            "endpoint_id",
            "deduplication_key",
            name="webhook_deliveries_endpoint_id_deduplication_key_key",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="webhook_deliveries_payload_check",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="webhook_deliveries_attempt_count_check",
        ),
        CheckConstraint(
            "length(deduplication_key) BETWEEN 1 AND 255",
            name="webhook_deliveries_deduplication_key_check",
        ),
        CheckConstraint(
            "octet_length(payload_body) BETWEEN 1 AND 65536",
            name="webhook_deliveries_payload_body_size_check",
        ),
        CheckConstraint(
            "last_response_code IS NULL OR last_response_code BETWEEN 100 AND 599",
            name="webhook_deliveries_response_code_check",
        ),
        CheckConstraint(
            "last_response_body IS NULL OR length(last_response_body) <= 1024",
            name="webhook_deliveries_response_body_size_check",
        ),
        CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 100",
            name="webhook_deliveries_last_error_size_check",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="webhook_deliveries_lease_check",
        ),
        CheckConstraint(
            "(status IN ('pending', 'retrying') AND next_attempt_at IS NOT NULL "
            "AND delivered_at IS NULL AND failed_at IS NULL) "
            "OR (status = 'delivered' AND next_attempt_at IS NULL "
            "AND delivered_at IS NOT NULL AND failed_at IS NULL) "
            "OR (status = 'failed' AND next_attempt_at IS NULL "
            "AND delivered_at IS NULL AND failed_at IS NOT NULL)",
            name="webhook_deliveries_terminal_state_check",
        ),
        CheckConstraint(
            "event_type = ANY ("
            "ARRAY['submission.created','verification.completed','mcp_request.completed',"
            "'payment.succeeded','payment.failed']::text[])",
            name="webhook_deliveries_event_type_check",
        ),
        Index(
            "webhook_due_idx",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status IN ('pending', 'retrying')"),
        ),
        {"comment": "Retryable submission/verification/payment event sent to a webhook."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        ENUM(
            DeliveryStatus,
            name="delivery_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'pending'::delivery_status"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_code: Mapped[int | None] = mapped_column(Integer)
    last_response_body: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
