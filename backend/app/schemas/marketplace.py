from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.claim import ClaimStatus
from app.models.task import BountyAction, SocialPlatform
from app.schemas.task import ProofType


class EligibleBountyResponse(BaseModel):
    bounty_id: UUID
    task_id: UUID
    task_title: str
    task_description: str
    bounty_title: str
    instructions: str
    platform: SocialPlatform
    action: BountyAction
    reward_minor: int
    currency: str
    effective_deadline: datetime | None
    proof_requirements: list[ProofType]
    remaining_slots: int
    social_account_id: UUID


class BountyClaimCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freelancer_id: UUID
    social_account_id: UUID


class BountyClaimResponse(BaseModel):
    id: UUID
    bounty_id: UUID
    freelancer_id: UUID
    social_account_id: UUID
    platform: SocialPlatform
    status: ClaimStatus
    reward_minor: int
    currency: str
    claimed_at: datetime
    claim_expires_at: datetime | None
    updated_at: datetime
