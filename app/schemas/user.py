from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator, model_validator


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str
    can_create_tasks: bool
    can_work_tasks: bool
    bio: str | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name cannot be empty")
        return value

    @field_validator("bio")
    @classmethod
    def normalize_bio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if not self.can_create_tasks and not self.can_work_tasks:
            raise ValueError("at least one user capability must be enabled")
        return self


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    can_create_tasks: bool
    can_work_tasks: bool
    bio: str | None
    created_at: datetime
    updated_at: datetime
