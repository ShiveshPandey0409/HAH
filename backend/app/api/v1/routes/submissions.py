from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.submission import VerificationMethod
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionResponse,
    SubmissionVerificationCreate,
    VerificationCommand,
)
from app.services.submissions import (
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionValidationError,
    create_submission_and_commit,
    verify_submission_and_commit,
)

router = APIRouter(tags=["submissions"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def _submission_http_error(error: Exception) -> HTTPException:
    if isinstance(error, SubmissionNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    if isinstance(error, SubmissionConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, SubmissionValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post(
    "/claims/{claim_id}/submissions",
    response_model=SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_submission_endpoint(
    claim_id: UUID,
    data: SubmissionCreate,
    session: SessionDependency,
) -> SubmissionResponse:
    try:
        return await create_submission_and_commit(session, claim_id, data)
    except (
        SubmissionNotFoundError,
        SubmissionConflictError,
        SubmissionValidationError,
        DBAPIError,
    ) as error:
        raise _submission_http_error(error) from error


@router.post(
    "/submissions/{submission_id}/verification",
    response_model=SubmissionResponse,
)
async def verify_submission_endpoint(
    submission_id: UUID,
    data: SubmissionVerificationCreate,
    session: SessionDependency,
) -> SubmissionResponse:
    command = VerificationCommand(
        result=data.result,
        checks=data.checks,
        failure_reason=data.failure_reason,
    )
    try:
        return await verify_submission_and_commit(
            session,
            submission_id,
            command,
            method=VerificationMethod.MANUAL,
            verifier_user_id=data.verifier_user_id,
            authorized_creator_id=data.verifier_user_id,
        )
    except (
        SubmissionNotFoundError,
        SubmissionConflictError,
        SubmissionValidationError,
        DBAPIError,
    ) as error:
        raise _submission_http_error(error) from error
