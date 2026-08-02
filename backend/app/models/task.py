from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TaskStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class BountyStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class SocialPlatform(StrEnum):
    REDDIT = "reddit"
    LINKEDIN = "linkedin"


class BountyAction(StrEnum):
    POST = "post"
    COMMENT = "comment"


class InfluenceMetric(StrEnum):
    FOLLOWERS = "followers"
    KARMA = "karma"


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="tasks_title_check"),
        CheckConstraint("total_budget_minor > 0", name="tasks_total_budget_minor_check"),
        CheckConstraint("currency = upper(currency)", name="tasks_currency_check"),
        CheckConstraint("created_via IN ('manual', 'mcp')", name="tasks_created_via_check"),
        CheckConstraint(
            "deadline_at IS NULL OR deadline_at > created_at",
            name="tasks_deadline_at_check",
        ),
        {"comment": "Creator-owned campaign with a total budget."},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    creator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    total_budget_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        ENUM(
            TaskStatus,
            name="task_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'draft'::task_status"),
    )
    created_via: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'manual'"),
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class Bounty(Base):
    __tablename__ = "bounties"
    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="bounties_title_check"),
        CheckConstraint("reward_minor > 0", name="bounties_reward_minor_check"),
        CheckConstraint("slots_total > 0", name="bounties_slots_total_check"),
        CheckConstraint("min_influence >= 0", name="bounties_min_influence_check"),
        CheckConstraint(
            "max_influence IS NULL OR max_influence >= 0",
            name="bounties_max_influence_check",
        ),
        CheckConstraint(
            "max_influence IS NULL OR min_influence <= max_influence",
            name="bounties_influence_range_check",
        ),
        CheckConstraint("jsonb_typeof(proof_requirements) = 'array'", name="bounties_proof_array"),
        CheckConstraint(
            "jsonb_array_length(proof_requirements) > 0",
            name="bounties_proof_nonempty",
        ),
        CheckConstraint(
            'proof_requirements <@ \'["url", "screenshot", "image"]\'::jsonb',
            name="bounties_proof_allowed",
        ),
        CheckConstraint(
            "hah_jsonb_text_array_is_unique(proof_requirements)",
            name="bounties_proof_requirements_unique",
        ),
        CheckConstraint(
            "(platform = 'reddit' AND influence_metric IN ('followers', 'karma')) OR "
            "(platform = 'linkedin' AND influence_metric = 'followers')",
            name="bounties_platform_metric_check",
        ),
        Index("bounties_feed_idx", "platform", "status", "deadline_at"),
        {
            "comment": "Paid Reddit/LinkedIn post or comment subtask with eligibility "
            "and proof rules."
        },
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SocialPlatform] = mapped_column(
        ENUM(
            SocialPlatform,
            name="social_platform",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    action: Mapped[BountyAction] = mapped_column(
        ENUM(
            BountyAction,
            name="bounty_action",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    reward_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    slots_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    influence_metric: Mapped[InfluenceMetric] = mapped_column(
        ENUM(
            InfluenceMetric,
            name="influence_metric",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    min_influence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    max_influence: Mapped[int | None] = mapped_column(BigInteger)
    proof_requirements: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[\"url\"]'::jsonb"),
    )
    status: Mapped[BountyStatus] = mapped_column(
        ENUM(
            BountyStatus,
            name="bounty_status",
            create_type=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        server_default=text("'draft'::bounty_status"),
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
