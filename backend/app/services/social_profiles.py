from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.social import SocialAccount
from app.models.task import SocialPlatform
from app.models.user import User
from app.schemas.social import SocialProfilePutRequest, SocialProfileResponse
from app.services.enrichment import (
    EnrichmentInvalidResponseError,
    EnrichmentProvider,
    EnrichmentRejectedError,
    EnrichmentUnavailableError,
    validate_provider_result,
)


class SocialProfileUserNotFoundError(Exception):
    pass


class SocialProfileUserCannotWorkError(Exception):
    pass


class SocialProfileURLValidationError(Exception):
    pass


class SocialProfileConflictError(Exception):
    pass


_REDDIT_PATH = re.compile(r"^/(?:user|u)/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)
_LINKEDIN_PATH = re.compile(r"^/in/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)
_REDDIT_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com"})
_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
ENRICHMENT_TIMEOUT_SECONDS = 10.0


def normalize_social_profile_url(platform: SocialPlatform, profile_url: str) -> str:
    if not profile_url or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in profile_url
    ):
        raise SocialProfileURLValidationError("profile_url is not a valid public profile URL")

    try:
        parsed = urlsplit(profile_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise SocialProfileURLValidationError(
            "profile_url is not a valid public profile URL"
        ) from error

    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or ":" in parsed.netloc
    ):
        raise SocialProfileURLValidationError("profile_url is not a valid public profile URL")

    hostname = hostname.lower()
    if platform == SocialPlatform.REDDIT:
        match = _REDDIT_PATH.fullmatch(parsed.path)
        if hostname not in _REDDIT_HOSTS or match is None:
            raise SocialProfileURLValidationError(
                "profile_url does not match the selected platform"
            )
        return f"https://www.reddit.com/user/{match.group(1).lower()}/"

    if platform == SocialPlatform.LINKEDIN:
        match = _LINKEDIN_PATH.fullmatch(parsed.path)
        if hostname not in _LINKEDIN_HOSTS or match is None:
            raise SocialProfileURLValidationError(
                "profile_url does not match the selected platform"
            )
        return f"https://www.linkedin.com/in/{match.group(1).lower()}/"

    raise SocialProfileURLValidationError("unsupported social platform")


def _sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


def _constraint_name(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "constraint_name", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)


def _profile_response(profile: SocialAccount) -> SocialProfileResponse:
    return SocialProfileResponse.model_validate(profile)


async def _load_profile(session: AsyncSession, profile_id: UUID) -> SocialAccount:
    profile = await session.get(SocialAccount, profile_id)
    if profile is None:
        raise SocialProfileConflictError("social profile was superseded")
    return profile


async def put_social_profile(
    session: AsyncSession,
    *,
    user_id: UUID,
    platform: SocialPlatform,
    data: SocialProfilePutRequest,
    provider: EnrichmentProvider,
) -> SocialProfileResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise SocialProfileUserNotFoundError
    if not user.can_work_tasks:
        raise SocialProfileUserCannotWorkError

    normalized_url = normalize_social_profile_url(platform, data.profile_url)
    enrichment_request_id = uuid4()
    pending_values = {
        "profile_url": normalized_url,
        "follower_count": None,
        "following_count": None,
        "reddit_post_karma": None,
        "reddit_comment_karma": None,
        "account_created_at": None,
        "is_verified": False,
        "verified_at": None,
        "enrichment_provider": None,
        "enriched_at": None,
        "enrichment_request_id": enrichment_request_id,
        "enrichment_data": {},
    }
    pending_insert = pg_insert(SocialAccount).values(
        user_id=user_id,
        platform=platform,
        **pending_values,
    )
    same_profile_url = SocialAccount.profile_url == pending_insert.excluded.profile_url
    pending_update_values = {
        "profile_url": pending_insert.excluded.profile_url,
        "enrichment_request_id": pending_insert.excluded.enrichment_request_id,
    }
    for field_name in (
        "follower_count",
        "following_count",
        "reddit_post_karma",
        "reddit_comment_karma",
        "account_created_at",
        "is_verified",
        "verified_at",
        "enrichment_provider",
        "enriched_at",
        "enrichment_data",
    ):
        pending_update_values[field_name] = case(
            (same_profile_url, getattr(SocialAccount, field_name)),
            else_=getattr(pending_insert.excluded, field_name),
        )

    pending_statement = pending_insert.on_conflict_do_update(
        index_elements=[SocialAccount.user_id, SocialAccount.platform],
        set_=pending_update_values,
    ).returning(SocialAccount.id)

    try:
        pending = (await session.execute(pending_statement)).one()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if (
            _sqlstate(error) == "23505"
            and _constraint_name(error) == "social_accounts_platform_profile_url_key"
        ):
            raise SocialProfileConflictError(
                "social profile URL already belongs to another user"
            ) from error
        raise
    except Exception:
        await session.rollback()
        raise

    try:
        async with asyncio.timeout(ENRICHMENT_TIMEOUT_SECONDS):
            raw_result = await provider.enrich(platform=platform, profile_url=normalized_url)
    except (EnrichmentRejectedError, EnrichmentInvalidResponseError, EnrichmentUnavailableError):
        raise
    except TimeoutError as error:
        raise EnrichmentUnavailableError("enrichment provider is unavailable") from error

    result = validate_provider_result(raw_result, platform=platform)
    enriched_at = datetime.now(UTC)
    completed_statement = (
        update(SocialAccount)
        .where(
            SocialAccount.id == pending.id,
            SocialAccount.profile_url == normalized_url,
            SocialAccount.enrichment_request_id == enrichment_request_id,
        )
        .values(
            follower_count=result.follower_count,
            following_count=result.following_count,
            reddit_post_karma=result.reddit_post_karma,
            reddit_comment_karma=result.reddit_comment_karma,
            account_created_at=result.account_created_at,
            is_verified=result.is_verified,
            verified_at=enriched_at if result.is_verified else None,
            enrichment_provider=result.provider_name,
            enriched_at=enriched_at,
            enrichment_request_id=None,
            enrichment_data=result.public_data,
        )
        .returning(SocialAccount.id)
    )

    try:
        completed_profile_id = (await session.execute(completed_statement)).scalar_one_or_none()
        if completed_profile_id is None:
            await session.rollback()
            raise SocialProfileConflictError("social profile was superseded")
        await session.commit()
    except SocialProfileConflictError:
        raise
    except DBAPIError as error:
        await session.rollback()
        if _sqlstate(error) in {"22003", "22021", "22P05", "23514"}:
            raise EnrichmentInvalidResponseError(
                "enrichment provider returned data that could not be stored"
            ) from error
        raise
    except Exception:
        await session.rollback()
        raise

    session.expire_all()
    return _profile_response(await _load_profile(session, completed_profile_id))


async def list_social_profiles(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[SocialProfileResponse]:
    if await session.get(User, user_id) is None:
        raise SocialProfileUserNotFoundError

    profiles = list(
        (
            await session.scalars(
                select(SocialAccount)
                .where(SocialAccount.user_id == user_id)
                .order_by(SocialAccount.platform)
            )
        ).all()
    )
    return [_profile_response(profile) for profile in profiles]
