from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.task import (
    BountyAction,
    BountyStatus,
    InfluenceMetric,
    SocialPlatform,
    TaskStatus,
)

ProofType = Literal["url", "screenshot", "image"]
TaskCreationSource = Literal["manual", "mcp"]
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
POSTGRES_INTEGER_MAX = 2_147_483_647


def _trim_required(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} cannot be empty")
    return value


def _validate_aware_deadline(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline_at must include a timezone")
    return value


class BountyCreate(BaseModel):
    platform: SocialPlatform
    action: BountyAction
    title: str
    instructions: str
    reward_minor: int = Field(gt=0, le=POSTGRES_BIGINT_MAX)
    slot_count: int = Field(gt=0, le=POSTGRES_INTEGER_MAX)
    influence_metric: InfluenceMetric
    min_influence: int = Field(default=0, ge=0, le=POSTGRES_BIGINT_MAX)
    max_influence: int | None = Field(default=None, ge=0, le=POSTGRES_BIGINT_MAX)
    proof_requirements: list[ProofType] = Field(
        default_factory=lambda: ["url"],
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    deadline_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _trim_required(value, "title")

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, value: str) -> str:
        return _trim_required(value, "instructions")

    @field_validator("proof_requirements")
    @classmethod
    def validate_proof_requirements(cls, value: list[ProofType]) -> list[ProofType]:
        if len(value) != len(set(value)):
            raise ValueError("proof requirements cannot contain duplicates")
        return value

    @field_validator("deadline_at")
    @classmethod
    def validate_deadline(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_deadline(value)

    @model_validator(mode="after")
    def validate_influence(self) -> Self:
        if self.max_influence is not None and self.min_influence > self.max_influence:
            raise ValueError("min_influence cannot exceed max_influence")
        if (
            self.platform == SocialPlatform.LINKEDIN
            and self.influence_metric != InfluenceMetric.FOLLOWERS
        ):
            raise ValueError("LinkedIn bounties must use followers as the influence metric")
        return self


class TaskInput(BaseModel):
    title: str
    description: str
    total_budget_minor: int = Field(gt=0, le=POSTGRES_BIGINT_MAX)
    currency: str
    deadline_at: datetime | None = None
    bounties: list[BountyCreate] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _trim_required(value, "title")

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _trim_required(value, "description")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha() or not value.isascii():
            raise ValueError("currency must be a three-letter ASCII code")
        return value

    @field_validator("deadline_at")
    @classmethod
    def validate_deadline(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_deadline(value)

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        allocated = sum(bounty.reward_minor * bounty.slot_count for bounty in self.bounties)
        if allocated > self.total_budget_minor:
            raise ValueError("bounties cannot allocate more than the task budget")
        if self.deadline_at is not None:
            for bounty in self.bounties:
                if bounty.deadline_at is not None and bounty.deadline_at > self.deadline_at:
                    raise ValueError("a bounty deadline cannot be after the task deadline")
        return self


class MCPTaskCreateInput(TaskInput):
    pass


class TaskCreate(TaskInput):
    creator_id: UUID

    @model_validator(mode="after")
    def validate_future_deadlines(self) -> Self:
        now = datetime.now(UTC)
        if self.deadline_at is not None and self.deadline_at <= now:
            raise ValueError("deadline_at must be in the future")
        if any(
            bounty.deadline_at is not None and bounty.deadline_at <= now for bounty in self.bounties
        ):
            raise ValueError("bounty deadline_at must be in the future")
        return self


class BountyResponse(BaseModel):
    id: UUID
    platform: SocialPlatform
    action: BountyAction
    title: str
    instructions: str
    reward_minor: int
    slot_count: int
    influence_metric: InfluenceMetric
    min_influence: int
    max_influence: int | None
    proof_requirements: list[ProofType]
    status: BountyStatus
    deadline_at: datetime | None
    claim_count: int
    remaining_slots: int
    created_at: datetime
    updated_at: datetime


class TaskResponse(BaseModel):
    id: UUID
    creator_id: UUID
    title: str
    description: str
    total_budget_minor: int
    allocated_budget_minor: int
    remaining_budget_minor: int
    currency: str
    status: TaskStatus
    created_via: TaskCreationSource
    deadline_at: datetime | None
    bounties: list[BountyResponse]
    created_at: datetime
    updated_at: datetime
