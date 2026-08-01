from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.task import SocialPlatform


class ClaimStatus(StrEnum):
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    REVIEWING = "reviewing"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class BountyClaim(Base):
    __tablename__ = "bounty_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["social_account_id", "freelancer_id", "platform"],
            ["social_accounts.id", "social_accounts.user_id", "social_accounts.platform"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("bounty_id", "freelancer_id"),
        CheckConstraint(
            "claim_expires_at IS NULL OR claim_expires_at > claimed_at",
            name="bounty_claims_claim_expires_at_check",
        ),
        CheckConstraint("reward_minor > 0", name="bounty_claims_reward_minor_check"),
        CheckConstraint("currency = upper(currency)", name="bounty_claims_currency_check"),
        Index("claims_capacity_idx", "bounty_id", "status"),
        Index("claims_freelancer_idx", "freelancer_id", "status"),
        {"comment": "One freelancer reservation/slot and its work lifecycle."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    bounty_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bounties.id", ondelete="RESTRICT"),
        nullable=False,
    )
    freelancer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    social_account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    platform: Mapped[SocialPlatform] = mapped_column(
        ENUM(
            SocialPlatform,
            name="social_platform",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    status: Mapped[ClaimStatus] = mapped_column(
        ENUM(
            ClaimStatus,
            name="claim_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'claimed'::claim_status"),
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    reward_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
