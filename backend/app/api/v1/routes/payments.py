from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import AuthenticatedSessionDependency
from app.db.session import get_db_session
from app.schemas.payment import PaymentAuthorizationResponse, PaymentResponse, WalletResponse
from app.services.payments import (
    PaymentAuthorizationRequiredError,
    PaymentConflictError,
    PaymentNotFoundError,
    PaymentProviderUnavailableError,
    PaymentValidationError,
    PravaGatewayError,
    get_payment,
    get_submission_payment,
    get_task_payment_authorization,
    get_wallet,
    list_task_payments,
    refresh_task_payment_authorization_and_commit,
    retry_payment_and_commit,
    runtime_from_settings,
    start_task_payment_authorization_and_commit,
)

router = APIRouter(tags=["payments"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def _payment_http_error(error: Exception) -> HTTPException:
    if isinstance(error, PaymentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    if isinstance(error, (PaymentConflictError, PaymentAuthorizationRequiredError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, PaymentValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )
    if isinstance(error, PaymentProviderUnavailableError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Prava sandbox is not configured",
        )
    if isinstance(error, PravaGatewayError):
        return HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if error.retryable
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail=f"Prava sandbox request failed ({error.code})",
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post(
    "/tasks/{task_id}/payment-authorization",
    response_model=PaymentAuthorizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_task_payment_authorization_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> PaymentAuthorizationResponse:
    try:
        runtime = runtime_from_settings()
        return await start_task_payment_authorization_and_commit(
            session,
            task_id,
            creator_id=authenticated.user.id,
            runtime=runtime,
        )
    except (
        PaymentNotFoundError,
        PaymentConflictError,
        PaymentValidationError,
        PaymentProviderUnavailableError,
        PravaGatewayError,
        DBAPIError,
    ) as error:
        raise _payment_http_error(error) from error


@router.post(
    "/tasks/{task_id}/payment-authorization/refresh",
    response_model=PaymentAuthorizationResponse,
)
async def refresh_task_payment_authorization_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> PaymentAuthorizationResponse:
    try:
        runtime = runtime_from_settings()
        return await refresh_task_payment_authorization_and_commit(
            session,
            task_id,
            creator_id=authenticated.user.id,
            runtime=runtime,
        )
    except (
        PaymentNotFoundError,
        PaymentConflictError,
        PaymentValidationError,
        PaymentProviderUnavailableError,
        PravaGatewayError,
        DBAPIError,
    ) as error:
        raise _payment_http_error(error) from error


@router.get(
    "/tasks/{task_id}/payment-authorization",
    response_model=PaymentAuthorizationResponse,
)
async def get_task_payment_authorization_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> PaymentAuthorizationResponse:
    try:
        return await get_task_payment_authorization(
            session,
            task_id,
            creator_id=authenticated.user.id,
        )
    except PaymentNotFoundError as error:
        raise _payment_http_error(error) from error


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
async def get_payment_endpoint(
    payment_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> PaymentResponse:
    try:
        return await get_payment(
            session,
            payment_id,
            authorized_user_id=authenticated.user.id,
        )
    except PaymentNotFoundError as error:
        raise _payment_http_error(error) from error


@router.get("/submissions/{submission_id}/payment", response_model=PaymentResponse)
async def get_submission_payment_endpoint(
    submission_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> PaymentResponse:
    try:
        return await get_submission_payment(
            session,
            submission_id,
            authorized_user_id=authenticated.user.id,
        )
    except PaymentNotFoundError as error:
        raise _payment_http_error(error) from error


@router.get("/tasks/{task_id}/payments", response_model=list[PaymentResponse])
async def list_task_payments_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> list[PaymentResponse]:
    try:
        return await list_task_payments(
            session,
            task_id,
            creator_id=authenticated.user.id,
        )
    except PaymentNotFoundError as error:
        raise _payment_http_error(error) from error


@router.post("/payments/{payment_id}/retry", response_model=PaymentResponse)
async def retry_payment_endpoint(
    payment_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> PaymentResponse:
    try:
        return await retry_payment_and_commit(
            session,
            payment_id,
            creator_id=authenticated.user.id,
        )
    except (
        PaymentNotFoundError,
        PaymentConflictError,
        PaymentAuthorizationRequiredError,
        DBAPIError,
    ) as error:
        raise _payment_http_error(error) from error


@router.get("/wallet", response_model=WalletResponse)
async def get_wallet_endpoint(
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> WalletResponse:
    return await get_wallet(session, user_id=authenticated.user.id)
