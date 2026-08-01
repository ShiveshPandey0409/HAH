from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.webhook import (
    WebhookEndpointPutResponse,
    WebhookEndpointResponse,
    WebhookPutRequest,
)
from app.services.webhooks import (
    WebhookCreatorCannotCreateError,
    WebhookCreatorNotFoundError,
    WebhookDestinationResolutionError,
    WebhookDestinationValidationError,
    WebhookEndpointNotFoundError,
    WebhookRuntime,
    WebhookSecretError,
    configure_webhook_endpoint,
    get_webhook_endpoint,
)

router = APIRouter(tags=["webhooks"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def get_webhook_runtime(request: Request) -> WebhookRuntime:
    runtime = getattr(request.app.state, "webhook_runtime", None)
    if not isinstance(runtime, WebhookRuntime):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook configuration is unavailable",
        )
    return runtime


RuntimeDependency = Annotated[WebhookRuntime, Depends(get_webhook_runtime)]


def _webhook_http_error(error: Exception) -> HTTPException:
    if isinstance(error, WebhookCreatorNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Creator not found",
        )
    if isinstance(error, WebhookEndpointNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook endpoint not found",
        )
    if isinstance(error, WebhookCreatorCannotCreateError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="User is not allowed to create tasks",
        )
    if isinstance(
        error,
        (WebhookDestinationValidationError, WebhookDestinationResolutionError),
    ):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Webhook destination is not allowed",
        )
    if isinstance(error, WebhookSecretError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook configuration is unavailable",
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.put(
    "/users/{creator_id}/webhook",
    response_model=WebhookEndpointPutResponse,
    status_code=status.HTTP_200_OK,
)
async def put_webhook_endpoint(
    creator_id: UUID,
    data: WebhookPutRequest,
    session: SessionDependency,
    runtime: RuntimeDependency,
) -> WebhookEndpointPutResponse:
    try:
        return await configure_webhook_endpoint(
            session,
            creator_id=creator_id,
            data=data,
            cipher=runtime.cipher,
            resolver=runtime.resolver,
            policy=runtime.policy,
        )
    except (
        WebhookCreatorNotFoundError,
        WebhookCreatorCannotCreateError,
        WebhookDestinationValidationError,
        WebhookDestinationResolutionError,
        WebhookSecretError,
        DBAPIError,
    ) as error:
        raise _webhook_http_error(error) from error


@router.get(
    "/users/{creator_id}/webhook",
    response_model=WebhookEndpointResponse,
)
async def read_webhook_endpoint(
    creator_id: UUID,
    session: SessionDependency,
    runtime: RuntimeDependency,
) -> WebhookEndpointResponse:
    try:
        return await get_webhook_endpoint(
            session,
            creator_id=creator_id,
            cipher=runtime.cipher,
            policy=runtime.policy,
        )
    except (
        WebhookCreatorNotFoundError,
        WebhookCreatorCannotCreateError,
        WebhookEndpointNotFoundError,
        WebhookSecretError,
        DBAPIError,
    ) as error:
        raise _webhook_http_error(error) from error
