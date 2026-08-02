from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.redaction import redact_sensitive_data
from app.models.claim import ClaimStatus
from app.models.submission import VerificationMethod, VerificationStatus
from app.models.task import SocialPlatform
from app.schemas.task import ProofType

MAX_PROOF_URL_LENGTH = 2_048
MAX_PROOF_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_VERIFICATION_CHECKS_BYTES = 16_384


def _has_forbidden_characters(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    )


def _normalize_https_url(value: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_PROOF_URL_LENGTH or _has_forbidden_characters(value):
        raise ValueError("url must be a valid HTTPS URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("url must be a valid HTTPS URL") from error
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("url must be a valid HTTPS URL")
    return value


class SubmissionProofCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proof_type: ProofType
    url: str | None = None
    upload_id: UUID | None = None

    @field_validator("url", mode="before")
    @classmethod
    def validate_url_type(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("url must be a string")
        return value

    @model_validator(mode="after")
    def validate_proof_shape(self) -> Self:
        if self.proof_type == "url":
            if self.url is None:
                raise ValueError("url proof requires url")
            self.url = _normalize_https_url(self.url)
            if self.upload_id is not None:
                raise ValueError("url proof cannot contain upload_id")
            return self

        if self.upload_id is None:
            raise ValueError("screenshot and image proofs require upload_id")
        if self.url is not None:
            raise ValueError("screenshot and image proofs cannot contain url")
        return self


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freelancer_id: UUID
    proofs: list[SubmissionProofCreate] = Field(min_length=1, max_length=3)

    @field_validator("proofs")
    @classmethod
    def validate_unique_proof_types(
        cls,
        value: list[SubmissionProofCreate],
    ) -> list[SubmissionProofCreate]:
        proof_types = [proof.proof_type for proof in value]
        if len(proof_types) != len(set(proof_types)):
            raise ValueError("proof types cannot contain duplicates")
        return value


class SubmissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proofs: list[SubmissionProofCreate] = Field(min_length=1, max_length=3)

    @field_validator("proofs")
    @classmethod
    def validate_unique_proof_types(
        cls,
        value: list[SubmissionProofCreate],
    ) -> list[SubmissionProofCreate]:
        proof_types = [proof.proof_type for proof in value]
        if len(proof_types) != len(set(proof_types)):
            raise ValueError("proof types cannot contain duplicates")
        return value


VerificationResult = Literal["passed", "failed", "review_required"]


class VerificationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: VerificationResult
    checks: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("checks")
    @classmethod
    def validate_and_redact_checks(cls, value: dict[str, Any]) -> dict[str, Any]:
        redacted = redact_sensitive_data(value)
        try:
            serialized = json.dumps(
                redacted,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("checks must contain valid JSON data") from error
        if len(serialized) > MAX_VERIFICATION_CHECKS_BYTES:
            raise ValueError("checks are too large")
        return redacted

    @field_validator("failure_reason", mode="before")
    @classmethod
    def normalize_failure_reason(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("failure_reason must be a string")
        value = value.strip()
        if not value:
            return None
        redacted = redact_sensitive_data(value)
        if not isinstance(redacted, str):
            raise ValueError("failure_reason must be a string")
        return redacted

    @model_validator(mode="after")
    def validate_result_fields(self) -> Self:
        if self.result == "passed" and self.failure_reason is not None:
            raise ValueError("passed verification cannot contain failure_reason")
        if self.result == "failed" and self.failure_reason is None:
            raise ValueError("failed verification requires failure_reason")
        return self


class SubmissionVerificationCreate(VerificationCommand):
    verifier_user_id: UUID
    method: Literal["manual"]


class SubmissionVerificationRequest(VerificationCommand):
    pass


class SubmissionProofResponse(BaseModel):
    id: UUID
    proof_type: ProofType
    url: str | None
    storage_key: str | None
    upload_id: UUID | None
    mime_type: str | None
    sha256: str | None
    size_bytes: int | None
    content_url: str | None


class ProofUploadResponse(BaseModel):
    upload_id: UUID
    claim_id: UUID
    proof_type: Literal["screenshot", "image"]
    mime_type: str
    sha256: str
    size_bytes: int
    created_at: datetime


class SubmissionResponse(BaseModel):
    id: UUID
    claim_id: UUID
    revision: int
    proofs: list[SubmissionProofResponse]
    verification_method: VerificationMethod | None
    verification_status: VerificationStatus
    checks: dict[str, Any]
    verifier_user_id: UUID | None
    failure_reason: str | None
    claim_status: ClaimStatus
    submitted_at: datetime
    verified_at: datetime | None
    updated_at: datetime


class WorkClaimResponse(BaseModel):
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
    task_id: UUID
    task_title: str
    bounty_title: str
    instructions: str
    proof_requirements: list[ProofType]
    submission: SubmissionResponse | None
