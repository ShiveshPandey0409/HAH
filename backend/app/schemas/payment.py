from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from app.models.payment import AuthorizationStatus, PaymentStatus


class PaymentAuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    task_id: UUID
    pool_id: UUID
    provider: str
    status: AuthorizationStatus
    per_payment_cap_minor: int = Field(gt=0)
    total_cap_minor: int = Field(gt=0)
    pool_cap_minor: int = Field(gt=0)
    pool_funded_once: bool
    pool_allocated_minor: int = Field(ge=0)
    pool_available_minor: int = Field(ge=0)
    used_minor: int = Field(ge=0)
    max_payments: int | None = Field(default=None, gt=0)
    payments_used: int = Field(ge=0)
    currency: str
    provider_authorization_ref: str | None = None
    funding_status: PaymentStatus
    provider_funding_transaction_ref: str | None = None
    funding_failure_code: str | None = None
    funding_failure_message: str | None = None
    funded_at: datetime | None = None
    blocked_minor: int = Field(ge=0)
    remaining_minor: int = Field(ge=0)
    other_tasks_blocked_minor: int = Field(ge=0)
    total_creator_blocked_minor: int = Field(ge=0)
    additional_approval_required_minor: int = Field(ge=0)
    global_approved_minor: int = Field(ge=0)
    global_allocated_minor: int = Field(ge=0)
    global_available_minor: int = Field(ge=0)
    global_pending_approval_minor: int = Field(ge=0)
    reused_global_approval: bool
    approval_url: AnyHttpUrl | None = None
    approval_expires_at: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class GlobalPaymentAllowanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str
    approved_minor: int = Field(ge=0)
    allocated_minor: int = Field(ge=0)
    available_minor: int = Field(ge=0)
    pending_approval_minor: int = Field(ge=0)
    active_pool_count: int = Field(ge=0)
    pending_pool_count: int = Field(ge=0)


class PaymentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    task_id: UUID
    bounty_id: UUID
    claim_id: UUID
    submission_id: UUID
    payer_user_id: UUID
    payee_user_id: UUID
    provider: str
    amount_minor: int = Field(gt=0)
    currency: str
    status: PaymentStatus
    provider_transaction_ref: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    next_attempt_at: datetime | None = None
    attempt_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class WalletEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    payment_id: UUID
    amount_minor: int = Field(gt=0)
    currency: str
    entry_type: str
    created_at: datetime


class WalletBalanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str
    balance_minor: int = Field(ge=0)


class WalletResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    redeemable: bool = False
    balances: list[WalletBalanceResponse]
    entries: list[WalletEntryResponse]
