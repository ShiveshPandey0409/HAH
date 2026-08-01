from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Text, func, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("btrim(display_name) <> ''", name="users_display_name_not_blank"),
        CheckConstraint("can_create_tasks OR can_work_tasks", name="users_has_capability"),
        CheckConstraint(
            "prava_account_status IS NULL OR "
            "prava_account_status IN ('pending', 'active', 'disabled')",
            name="users_prava_account_status_valid",
        ),
        CheckConstraint(
            "(prava_account_ref IS NULL) = (prava_account_status IS NULL)",
            name="users_prava_account_pair",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    can_create_tasks: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    can_work_tasks: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    bio: Mapped[str | None] = mapped_column(Text)
    prava_account_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    prava_account_status: Mapped[str | None] = mapped_column(Text)
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
