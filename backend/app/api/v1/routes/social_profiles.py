from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    AUTHENTICATED_RESPONSES,
    AuthenticatedSessionDependency,
    require_self,
)
from app.db.session import get_db_session
from app.models.task import SocialPlatform
from app.schemas.social import SocialProfilePutRequest, SocialProfileResponse
from app.services.enrichment import (
    EnrichmentInvalidResponseError,
    EnrichmentProvider,
    EnrichmentRejectedError,
    EnrichmentUnavailableError,
    get_enrichment_provider,
)
from app.services.social_profiles import (
    SocialProfileConflictError,
    SocialProfileURLValidationError,
    SocialProfileUserCannotWorkError,
    SocialProfileUserNotFoundError,
    list_social_profiles,
    put_social_profile,
)

router = APIRouter(tags=["social profiles"], responses=AUTHENTICATED_RESPONSES)
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]
ProviderDependency = Annotated[EnrichmentProvider, Depends(get_enrichment_provider)]


def _social_profile_http_error(error: Exception) -> HTTPException:
    if isinstance(error, SocialProfileUserNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if isinstance(error, SocialProfileConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, SocialProfileUserCannotWorkError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="User is not allowed to work tasks",
        )
    if isinstance(error, SocialProfileURLValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )
    if isinstance(error, (EnrichmentRejectedError, EnrichmentInvalidResponseError)):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Social profile enrichment failed",
        )
    if isinstance(error, EnrichmentUnavailableError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Social profile enrichment is unavailable",
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.put(
    "/users/{user_id}/social-profiles/{platform}",
    response_model=SocialProfileResponse,
    status_code=status.HTTP_200_OK,
)
async def put_social_profile_endpoint(
    user_id: UUID,
    platform: SocialPlatform,
    data: SocialProfilePutRequest,
    session: SessionDependency,
    provider: ProviderDependency,
    authenticated: AuthenticatedSessionDependency,
) -> SocialProfileResponse:
    require_self(authenticated, user_id)
    try:
        return await put_social_profile(
            session,
            user_id=user_id,
            platform=platform,
            data=data,
            provider=provider,
        )
    except (
        SocialProfileUserNotFoundError,
        SocialProfileConflictError,
        SocialProfileUserCannotWorkError,
        SocialProfileURLValidationError,
        EnrichmentRejectedError,
        EnrichmentInvalidResponseError,
        EnrichmentUnavailableError,
        DBAPIError,
    ) as error:
        raise _social_profile_http_error(error) from error


@router.get(
    "/users/{user_id}/social-profiles",
    response_model=list[SocialProfileResponse],
)
async def list_social_profiles_endpoint(
    user_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> list[SocialProfileResponse]:
    require_self(authenticated, user_id)
    try:
        return await list_social_profiles(session, user_id=user_id)
    except (SocialProfileUserNotFoundError, DBAPIError) as error:
        raise _social_profile_http_error(error) from error
