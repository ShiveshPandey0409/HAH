from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VerificationMethod(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    MCP = "mcp"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    REVIEW_REQUIRED = "review_required"


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "revision",
            name="submissions_claim_id_revision_key",
        ),
        CheckConstraint("revision > 0", name="submissions_revision_check"),
        CheckConstraint(
            "jsonb_typeof(verification_checks) = 'object'",
            name="submissions_verification_checks_check",
        ),
        CheckConstraint(
            "(verification_status = 'pending' AND verification_method IS NULL "
            "AND verified_at IS NULL) OR "
            "(verification_status = 'review_required' AND verification_method IS NOT NULL "
            "AND verified_at IS NULL) OR "
            "(verification_status IN ('passed', 'failed') "
            "AND verification_method IS NOT NULL AND verified_at IS NOT NULL)",
            name="submissions_verification_state_check",
        ),
        {"comment": "A claim submission revision plus its current verification result."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    claim_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bounty_claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    note: Mapped[str | None] = mapped_column(Text)
    verification_method: Mapped[VerificationMethod | None] = mapped_column(
        ENUM(
            VerificationMethod,
            name="verification_method",
            create_type=False,
            values_callable=_enum_values,
        )
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        ENUM(
            VerificationStatus,
            name="verification_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'pending'::verification_status"),
    )
    verification_checks: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    verifier_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    verification_note: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SubmissionProof(Base):
    __tablename__ = "submission_proofs"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "kind",
            name="submission_proofs_submission_id_kind_key",
        ),
        CheckConstraint(
            "kind IN ('url', 'screenshot', 'image')",
            name="submission_proofs_kind_check",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="submission_proofs_metadata_check",
        ),
        CheckConstraint(
            "(kind = 'url' AND external_url IS NOT NULL AND storage_key IS NULL "
            "AND mime_type IS NULL AND sha256 IS NULL) OR "
            "(kind IN ('screenshot', 'image') AND external_url IS NULL "
            "AND storage_key IS NOT NULL)",
            name="submission_proofs_shape_check",
        ),
        CheckConstraint(
            "kind <> 'url' OR external_url ~* '^https://[^[:space:]]+$'",
            name="submission_proofs_https_url_check",
        ),
        CheckConstraint(
            "kind = 'url' OR btrim(storage_key) <> ''",
            name="submission_proofs_storage_key_nonblank_check",
        ),
        CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_proofs_sha256_check",
        ),
        {"comment": "One or more URLs/screenshots/images attached to a submission."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    proof_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
