from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.redaction import redact_sensitive_data
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


class HackathonSelfAttestedEnrichmentProvider:
    """URL-only admission for minimum-threshold hackathon tasks.

    Influence remains zero so this mode cannot satisfy tasks that require real
    follower or karma thresholds.
    """

    async def enrich(
        self,
        *,
        platform: SocialPlatform,
        profile_url: str,
    ) -> EnrichmentResult:
        public_data = {
            "source": "hackathon-self-attested",
            "verification": "normalized-public-profile-url-only",
            "profile_url": profile_url,
        }
        if platform == SocialPlatform.REDDIT:
            return EnrichmentResult(
                provider_name="hackathon-self-attested",
                is_verified=True,
                follower_count=0,
                following_count=0,
                reddit_post_karma=0,
                reddit_comment_karma=0,
                public_data=public_data,
            )
        return EnrichmentResult(
            provider_name="hackathon-self-attested",
            is_verified=True,
            follower_count=0,
            following_count=0,
            public_data=public_data,
        )


_DEFAULT_PROVIDER = UnavailableEnrichmentProvider()


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
        if result.is_verified and result.follower_count is None:
            raise EnrichmentInvalidResponseError("verified profile metrics are incomplete")
    elif result.is_verified and (
        result.reddit_post_karma is None and result.reddit_comment_karma is None
    ):
        # Reddit eligibility may use karma without a follower metric. Require a
        # provider-validated karma signal while allowing follower_count to be absent.
        raise EnrichmentInvalidResponseError("verified Reddit karma is incomplete")

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
    return redact_sensitive_data(value)
