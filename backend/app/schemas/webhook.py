from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models.integration import IntegrationStatus
from app.schemas.task import ProofType


class WebhookEventType(StrEnum):
    SUBMISSION_CREATED = "submission.created"
    VERIFICATION_COMPLETED = "verification.completed"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    MCP_REQUEST_COMPLETED = "mcp_request.completed"


CURRENT_WEBHOOK_EVENT_TYPES = frozenset(
    {
        WebhookEventType.SUBMISSION_CREATED,
        WebhookEventType.VERIFICATION_COMPLETED,
        WebhookEventType.PAYMENT_SUCCEEDED,
        WebhookEventType.PAYMENT_FAILED,
        WebhookEventType.MCP_REQUEST_COMPLETED,
    }
)


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class WebhookPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    subscribed_events: list[WebhookEventType] = Field(
        default_factory=list,
        max_length=len(WebhookEventType),
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("subscribed_events")
    @classmethod
    def validate_subscriptions(
        cls,
        value: list[WebhookEventType],
    ) -> list[WebhookEventType]:
        if len(value) != len(set(value)):
            raise ValueError("subscribed_events cannot contain duplicates")
        return sorted(value, key=lambda item: item.value)


class WebhookDeliveryConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    success_statuses: Literal["200-299"] = "200-299"
    max_attempts: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)


class WebhookEndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    creator_id: UUID
    url: str
    subscribed_events: list[WebhookEventType]
    status: IntegrationStatus
    delivery: WebhookDeliveryConfiguration
    created_at: datetime
    updated_at: datetime


class WebhookEndpointPutResponse(WebhookEndpointResponse):
    signing_secret: SecretStr = Field(repr=False)

    @field_serializer("signing_secret", when_used="json")
    def serialize_signing_secret(self, value: SecretStr) -> str:
        return value.get_secret_value()


class MCPWebhookStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured: bool
    webhook: WebhookEndpointResponse | None = None


class SubmissionCreatedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: UUID
    claim_id: UUID
    bounty_id: UUID
    task_id: UUID
    freelancer_id: UUID
    revision: int = Field(gt=0)
    submitted_at: datetime
    proof_types: list[ProofType] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    submission_url: str = Field(
        pattern=(
            r"^/v1/submissions/[0-9a-f]{8}-[0-9a-f]{4}-"
            r"[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )

    @field_validator("submitted_at")
    @classmethod
    def validate_submitted_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "submitted_at")

    @field_validator("proof_types")
    @classmethod
    def validate_proof_types(cls, value: list[ProofType]) -> list[ProofType]:
        if len(value) != len(set(value)):
            raise ValueError("proof_types cannot contain duplicates")
        return sorted(value)


PublicReasonCode = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"),
]


class VerificationCompletedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: UUID
    claim_id: UUID
    bounty_id: UUID
    task_id: UUID
    revision: int = Field(gt=0)
    method: Literal["automatic", "manual", "mcp"]
    status: Literal["passed", "failed", "review_required"]
    verified_at: datetime
    reason_code: PublicReasonCode | None = None

    @field_validator("verified_at")
    @classmethod
    def validate_verified_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "verified_at")

    @model_validator(mode="after")
    def validate_reason(self) -> VerificationCompletedData:
        if self.status == "passed" and self.reason_code is not None:
            raise ValueError("passed verification cannot include reason_code")
        return self


class MCPRequestCompletedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    method: Literal["create_task", "verify_submission"]
    status: Literal["succeeded", "failed"]
    completed_at: datetime
    task_id: UUID | None = None
    submission_id: UUID | None = None
    error_code: PublicReasonCode | None = None

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "completed_at")

    @model_validator(mode="after")
    def validate_result(self) -> MCPRequestCompletedData:
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful MCP request cannot include error_code")
        if self.method == "create_task" and self.submission_id is not None:
            raise ValueError("create_task result cannot include submission_id")
        if self.method == "verify_submission" and self.task_id is not None:
            raise ValueError("verify_submission result cannot include task_id")
        return self


class PaymentCompletedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payment_id: UUID
    submission_id: UUID
    claim_id: UUID
    bounty_id: UUID
    task_id: UUID
    payee_user_id: UUID
    status: Literal["succeeded", "failed"]
    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    completed_at: datetime
    failure_code: PublicReasonCode | None = None

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "completed_at")

    @model_validator(mode="after")
    def validate_failure(self) -> PaymentCompletedData:
        if self.status == "succeeded" and self.failure_code is not None:
            raise ValueError("successful payment cannot include failure_code")
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("failed payment requires failure_code")
        return self


CurrentWebhookEventData = (
    SubmissionCreatedData
    | VerificationCompletedData
    | PaymentCompletedData
    | MCPRequestCompletedData
)


class WebhookEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    type: WebhookEventType
    created_at: datetime
    data: CurrentWebhookEventData

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_data_matches_type(self) -> WebhookEventEnvelope:
        expected_type = {
            WebhookEventType.SUBMISSION_CREATED: SubmissionCreatedData,
            WebhookEventType.VERIFICATION_COMPLETED: VerificationCompletedData,
            WebhookEventType.PAYMENT_SUCCEEDED: PaymentCompletedData,
            WebhookEventType.PAYMENT_FAILED: PaymentCompletedData,
            WebhookEventType.MCP_REQUEST_COMPLETED: MCPRequestCompletedData,
        }.get(self.type)
        if expected_type is None:
            raise ValueError("unsupported webhook event type")
        if not isinstance(self.data, expected_type):
            raise ValueError("webhook event data does not match event type")
        return self
