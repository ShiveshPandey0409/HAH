from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuthorizationStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentStatus(StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class PaymentAuthorization(Base):
    __tablename__ = "payment_authorizations"
    __table_args__ = (
        UniqueConstraint("task_id", name="payment_authorizations_task_id_key"),
        Index("payment_authorizations_pool_idx", "pool_id", "created_at"),
        CheckConstraint(
            "total_cap_minor <= pool_cap_minor",
            name="payment_authorizations_pool_cap_check",
        ),
        CheckConstraint(
            "provider_customer_ref IS NULL OR "
            "(provider_customer_ref = btrim(provider_customer_ref) "
            "AND provider_customer_ref <> '')",
            name="payment_authorizations_customer_ref_check",
        ),
        CheckConstraint(
            "provider_session_ref IS NULL OR "
            "(provider_session_ref = btrim(provider_session_ref) "
            "AND provider_session_ref <> '')",
            name="payment_authorizations_session_ref_check",
        ),
        {"comment": ("Task reservation against a reusable HAH-level Prava allowance pool.")},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pool_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    pool_cap_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'prava'"),
    )
    provider_customer_ref: Mapped[str | None] = mapped_column(Text)
    provider_session_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    provider_session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_authorization_ref: Mapped[str | None] = mapped_column(Text)
    funding_status: Mapped[PaymentStatus] = mapped_column(
        ENUM(
            PaymentStatus,
            name="payment_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'created'::payment_status"),
    )
    funding_idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider_funding_transaction_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    funding_failure_code: Mapped[str | None] = mapped_column(Text)
    funding_failure_message: Mapped[str | None] = mapped_column(Text)
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[AuthorizationStatus] = mapped_column(
        ENUM(
            AuthorizationStatus,
            name="authorization_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'pending'::authorization_status"),
    )
    per_payment_cap_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_cap_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_minor: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    max_payments: Mapped[int | None] = mapped_column(Integer)
    payments_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index(
            "payments_due_idx",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status = 'created'"),
        ),
        Index(
            "payment_retry_idx",
            "status",
            "updated_at",
            postgresql_where=text("status IN ('created', 'processing', 'failed')"),
        ),
        {"comment": "One idempotent Prava-funded internal reward payment."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    authorization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_authorizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    bounty_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bounties.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bounty_claims.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    payer_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payee_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'prava'"),
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        ENUM(
            PaymentStatus,
            name="payment_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'created'::payment_status"),
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider_transaction_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_message: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint(
            "payment_id",
            "attempt_number",
            name="payment_attempts_payment_id_attempt_number_key",
        ),
        Index(
            "payment_attempts_provider_tx_unique",
            "provider_transaction_ref",
            unique=True,
            postgresql_where=text("provider_transaction_ref IS NOT NULL"),
        ),
        {"comment": "Safe Prava retry audit; card credentials are prohibited."},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_session_ref: Mapped[str | None] = mapped_column(Text)
    provider_transaction_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PaymentStatus] = mapped_column(
        ENUM(
            PaymentStatus,
            name="payment_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    request_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    response_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WalletEntry(Base):
    __tablename__ = "wallet_entries"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="wallet_entries_amount_check"),
        CheckConstraint("currency = upper(currency)", name="wallet_entries_currency_check"),
        CheckConstraint(
            "entry_type = 'task_reward'",
            name="wallet_entries_type_check",
        ),
        Index("wallet_entries_user_currency_idx", "user_id", "currency", "created_at"),
        {"comment": "Append-only internal hackathon wallet credits; no redemption."},
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
    payment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    entry_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'task_reward'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
