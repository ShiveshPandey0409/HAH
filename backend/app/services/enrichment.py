from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Protocol

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.models.task import SocialPlatform

POSTGRES_BIGINT_MIN = -9_223_372_036_854_775_808
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


class EnrichmentRejectedError(Exception):
    """The provider rejected a well-formed enrichment request."""


class EnrichmentInvalidResponseError(Exception):
    """The provider returned a result that cannot be safely persisted."""


class EnrichmentUnavailableError(Exception):
    """The configured provider could not service the request."""


class EnrichmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    is_verified: bool = Field(strict=True)
    follower_count: int | None = None
    following_count: int | None = None
    reddit_post_karma: int | None = None
    reddit_comment_karma: int | None = None
    account_created_at: datetime | None = None
    public_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider_name", mode="before")
    @classmethod
    def validate_provider_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("provider_name cannot be empty")
        return value

    @field_validator("follower_count", "following_count", mode="before")
    @classmethod
    def validate_nonnegative_count(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("public counts must be integers")
        if not 0 <= value <= POSTGRES_BIGINT_MAX:
            raise ValueError("public counts are outside the supported range")
        return value

    @field_validator("reddit_post_karma", "reddit_comment_karma", mode="before")
    @classmethod
    def validate_karma_component(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Reddit karma must be an integer")
        if not POSTGRES_BIGINT_MIN <= value <= POSTGRES_BIGINT_MAX:
            raise ValueError("Reddit karma is outside the supported range")
        return value

    @field_validator("account_created_at")
    @classmethod
    def validate_account_created_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("account_created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_karma_total(self) -> EnrichmentResult:
        if self.reddit_post_karma is None and self.reddit_comment_karma is None:
            return self
        total = (self.reddit_post_karma or 0) + (self.reddit_comment_karma or 0)
        if not POSTGRES_BIGINT_MIN <= total <= POSTGRES_BIGINT_MAX:
            raise ValueError("combined Reddit karma is outside the supported range")
        return self


class EnrichmentProvider(Protocol):
    async def enrich(
        self,
        *,
        platform: SocialPlatform,
        profile_url: str,
    ) -> EnrichmentResult: ...


class UnavailableEnrichmentProvider:
    async def enrich(
        self,
        *,
        platform: SocialPlatform,
        profile_url: str,
    ) -> EnrichmentResult:
        del platform, profile_url
        raise EnrichmentUnavailableError("enrichment provider is not configured")


_DEFAULT_PROVIDER = UnavailableEnrichmentProvider()
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "signingkey",
        "token",
    }
)


def get_enrichment_provider(request: Request) -> EnrichmentProvider:
    return getattr(request.app.state, "enrichment_provider", _DEFAULT_PROVIDER)


def validate_provider_result(
    raw_result: object,
    *,
    platform: SocialPlatform,
) -> EnrichmentResult:
    try:
        if isinstance(raw_result, EnrichmentResult):
            result = EnrichmentResult.model_validate(raw_result.model_dump())
        else:
            result = EnrichmentResult.model_validate(raw_result)
    except (TypeError, ValidationError, ValueError) as error:
        raise EnrichmentInvalidResponseError("enrichment provider returned invalid data") from error

    if platform == SocialPlatform.LINKEDIN:
        if result.reddit_post_karma is not None or result.reddit_comment_karma is not None:
            raise EnrichmentInvalidResponseError(
                "enrichment provider returned platform-incompatible data"
            )
        required_metrics = (result.follower_count,)
    else:
        required_metrics = (result.follower_count,)
        if result.is_verified and (
            result.reddit_post_karma is None and result.reddit_comment_karma is None
        ):
            raise EnrichmentInvalidResponseError("verified Reddit karma is incomplete")

    if result.is_verified and any(metric is None for metric in required_metrics):
        raise EnrichmentInvalidResponseError("verified profile metrics are incomplete")

    try:
        redacted = redact_public_data(result.public_data)
        json.dumps(redacted, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise EnrichmentInvalidResponseError(
            "enrichment provider returned invalid public data"
        ) from error
    result.public_data = redacted
    return result


def redact_public_data(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_public_data(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_public_data(item) for item in value]
    return value
