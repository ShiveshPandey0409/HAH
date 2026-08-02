from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    AUTHENTICATED_RESPONSES,
    AuthenticatedSessionDependency,
    require_self,
)
from app.db.session import get_db_session
from app.models.submission import VerificationMethod
from app.schemas.submission import (
    MAX_PROOF_UPLOAD_BYTES,
    ProofUploadResponse,
    SubmissionCreate,
    SubmissionCreateRequest,
    SubmissionResponse,
    SubmissionVerificationRequest,
    VerificationCommand,
    WorkClaimResponse,
)
from app.services.submissions import (
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionValidationError,
    create_proof_upload_and_commit,
    create_submission_and_commit,
    get_submission,
    get_submission_proof_content,
    list_freelancer_claims,
    list_task_submissions,
    verify_submission_and_commit,
)

router = APIRouter(tags=["submissions"], responses=AUTHENTICATED_RESPONSES)
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
    "/claims/{claim_id}/proof-uploads",
    response_model=ProofUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_proof_endpoint(
    claim_id: UUID,
    proof_type: Annotated[Literal["screenshot", "image"], Form()],
    file: Annotated[UploadFile, File()],
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> ProofUploadResponse:
    try:
        content = await file.read(MAX_PROOF_UPLOAD_BYTES + 1)
        return await create_proof_upload_and_commit(
            session,
            claim_id,
            freelancer_id=authenticated.user.id,
            proof_type=proof_type,
            content=content,
        )
    except (
        SubmissionNotFoundError,
        SubmissionConflictError,
        SubmissionValidationError,
        DBAPIError,
    ) as error:
        raise _submission_http_error(error) from error
    finally:
        await file.close()


@router.post(
    "/claims/{claim_id}/submissions",
    response_model=SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_submission_endpoint(
    claim_id: UUID,
    data: SubmissionCreateRequest,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> SubmissionResponse:
    command = SubmissionCreate(
        freelancer_id=authenticated.user.id,
        proofs=data.proofs,
    )
    try:
        return await create_submission_and_commit(session, claim_id, command)
    except (
        SubmissionNotFoundError,
        SubmissionConflictError,
        SubmissionValidationError,
        DBAPIError,
    ) as error:
        raise _submission_http_error(error) from error


@router.get("/submissions/{submission_id}", response_model=SubmissionResponse)
async def get_submission_endpoint(
    submission_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> SubmissionResponse:
    try:
        return await get_submission(
            session,
            submission_id,
            authorized_user_id=authenticated.user.id,
        )
    except SubmissionNotFoundError as error:
        raise _submission_http_error(error) from error


@router.get("/tasks/{task_id}/submissions", response_model=list[SubmissionResponse])
async def list_task_submissions_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> list[SubmissionResponse]:
    try:
        return await list_task_submissions(
            session,
            task_id,
            authorized_creator_id=authenticated.user.id,
        )
    except SubmissionNotFoundError as error:
        raise _submission_http_error(error) from error


@router.get(
    "/freelancers/{freelancer_id}/claims",
    response_model=list[WorkClaimResponse],
)
async def list_freelancer_claims_endpoint(
    freelancer_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> list[WorkClaimResponse]:
    require_self(authenticated, freelancer_id)
    return await list_freelancer_claims(session, freelancer_id)


@router.get("/submissions/{submission_id}/proofs/{proof_id}/content")
async def get_submission_proof_content_endpoint(
    submission_id: UUID,
    proof_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> Response:
    try:
        proof = await get_submission_proof_content(
            session,
            submission_id,
            proof_id,
            authorized_user_id=authenticated.user.id,
        )
    except SubmissionNotFoundError as error:
        raise _submission_http_error(error) from error
    return Response(
        content=proof.content,
        media_type=proof.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "ETag": f'"sha256:{proof.sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/submissions/{submission_id}/verification",
    response_model=SubmissionResponse,
)
async def verify_submission_endpoint(
    submission_id: UUID,
    data: SubmissionVerificationRequest,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
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
            verifier_user_id=authenticated.user.id,
            authorized_creator_id=authenticated.user.id,
        )
    except (
        SubmissionNotFoundError,
        SubmissionConflictError,
        SubmissionValidationError,
        DBAPIError,
    ) as error:
        raise _submission_http_error(error) from error
