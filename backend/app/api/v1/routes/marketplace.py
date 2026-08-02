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
from app.schemas.marketplace import (
    BountyClaimCreate,
    BountyClaimRequest,
    BountyClaimResponse,
    EligibleBountyResponse,
)
from app.services.marketplace import (
    MarketplaceConflictError,
    MarketplaceNotFoundError,
    MarketplaceValidationError,
    claim_bounty,
    get_eligible_bounties,
)

router = APIRouter(tags=["marketplace"], responses=AUTHENTICATED_RESPONSES)
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def _marketplace_http_error(error: Exception) -> HTTPException:
    if isinstance(error, MarketplaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, MarketplaceConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, MarketplaceValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/freelancers/{freelancer_id}/bounties",
    response_model=list[EligibleBountyResponse],
)
async def get_eligible_bounties_endpoint(
    freelancer_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> list[EligibleBountyResponse]:
    require_self(authenticated, freelancer_id)
    try:
        return await get_eligible_bounties(session, freelancer_id)
    except (MarketplaceNotFoundError, MarketplaceValidationError, DBAPIError) as error:
        raise _marketplace_http_error(error) from error


@router.post(
    "/bounties/{bounty_id}/claims",
    response_model=BountyClaimResponse,
    status_code=status.HTTP_201_CREATED,
)
async def claim_bounty_endpoint(
    bounty_id: UUID,
    data: BountyClaimRequest,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> BountyClaimResponse:
    command = BountyClaimCreate(
        freelancer_id=authenticated.user.id,
        social_account_id=data.social_account_id,
    )
    try:
        return await claim_bounty(session, bounty_id, command)
    except (
        MarketplaceNotFoundError,
        MarketplaceConflictError,
        MarketplaceValidationError,
        DBAPIError,
    ) as error:
        raise _marketplace_http_error(error) from error
