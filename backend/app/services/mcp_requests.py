from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.session import AsyncSessionFactory
from app.models.integration import MCPRequest, RequestStatus
from app.schemas.task import MCPTaskCreateInput, TaskCreate, TaskResponse
from app.services.api_clients import (
    TASKS_CREATE_SCOPE,
    APIClientPrincipal,
    require_api_scope,
)
from app.services.tasks import (
    CreatorCannotCreateTasksError,
    CreatorNotFoundError,
    TaskValidationError,
    create_task,
)

CREATE_TASK_METHOD = "create_task"
logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    pass


class MCPRequestExecutionError(Exception):
    def __init__(self, request_id: UUID, message: str) -> None:
        self.request_id = request_id
        super().__init__(f"{message} (request_id: {request_id})")


class MCPRequestValidationError(Exception):
    def __init__(self, request_id: UUID, error: ValidationError) -> None:
        self.request_id = request_id
        details = {
            "error": "validation_error",
            "request_id": str(request_id),
            "details": error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        }
        super().__init__(json.dumps(details, separators=(",", ":")))


class _TaskInputValidationError(Exception):
    def __init__(self, error: ValidationError) -> None:
        self.error = error


def _normalize_idempotency_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 200:
        raise ValueError("idempotency_key must contain between 1 and 200 characters")
    return value


async def _reserve_request(
    *,
    new_request_id: UUID,
    api_client_id: UUID,
    method: str,
    idempotency_key: str,
    request_data: dict[str, Any],
) -> UUID:
    async with AsyncSessionFactory() as session:
        request_id = await session.scalar(
            insert(MCPRequest)
            .values(
                id=new_request_id,
                api_client_id=api_client_id,
                method=method,
                idempotency_key=idempotency_key,
                status=RequestStatus.STARTED,
                request_data=request_data,
            )
            .on_conflict_do_nothing(
                index_elements=[MCPRequest.api_client_id, MCPRequest.idempotency_key]
            )
            .returning(MCPRequest.id)
        )
        if request_id is None:
            request_id = await session.scalar(
                select(MCPRequest.id).where(
                    MCPRequest.api_client_id == api_client_id,
                    MCPRequest.idempotency_key == idempotency_key,
                )
            )
        await session.commit()

    if request_id is None:
        raise RuntimeError("MCP request reservation could not be loaded")
    return request_id


def _safe_task_error(error: Exception) -> str:
    if isinstance(error, CreatorNotFoundError):
        return "task creator no longer exists"
    if isinstance(error, CreatorCannotCreateTasksError):
        return "API client owner cannot create tasks"
    if isinstance(error, TaskValidationError):
        return "task or bounty violates a database rule"
    return "task creation failed"


async def _mark_failed(request_id: UUID, error_message: str) -> None:
    async with AsyncSessionFactory() as session:
        request = await session.scalar(
            select(MCPRequest).where(MCPRequest.id == request_id).with_for_update()
        )
        if request is not None and request.status != RequestStatus.SUCCEEDED:
            request.status = RequestStatus.FAILED
            request.response_data = None
            request.error_message = error_message
            request.completed_at = datetime.now(UTC)
            await session.commit()


async def create_task_from_mcp(
    principal: APIClientPrincipal,
    *,
    idempotency_key: str,
    data: MCPTaskCreateInput,
) -> TaskResponse:
    require_api_scope(principal, TASKS_CREATE_SCOPE)
    normalized_key = _normalize_idempotency_key(idempotency_key)
    request_data = {
        "creator_id": str(principal.creator_id),
        **data.model_dump(mode="json"),
    }
    request_id = uuid4()

    try:
        request_id = await _reserve_request(
            new_request_id=request_id,
            api_client_id=principal.client_id,
            method=CREATE_TASK_METHOD,
            idempotency_key=normalized_key,
            request_data=request_data,
        )
        async with AsyncSessionFactory() as session:
            request = await session.scalar(
                select(MCPRequest).where(MCPRequest.id == request_id).with_for_update()
            )
            if request is None:
                raise RuntimeError("MCP request reservation disappeared")
            if request.method != CREATE_TASK_METHOD or request.request_data != request_data:
                raise IdempotencyConflictError(
                    "idempotency key was already used with different input"
                )
            if request.status == RequestStatus.SUCCEEDED:
                if not isinstance(request.response_data, dict):
                    raise RuntimeError("stored MCP response is invalid")
                return TaskResponse.model_validate(request.response_data)

            request.status = RequestStatus.STARTED
            request.error_message = None
            request.completed_at = None
            try:
                task_data = TaskCreate(
                    creator_id=principal.creator_id,
                    **data.model_dump(),
                )
            except ValidationError as error:
                raise _TaskInputValidationError(error) from error
            response = await create_task(
                session,
                task_data,
                creation_source="mcp",
            )
            request.status = RequestStatus.SUCCEEDED
            request.response_data = response.model_dump(mode="json")
            request.error_message = None
            request.task_id = response.id
            request.completed_at = datetime.now(UTC)
            await session.commit()
            return response
    except IdempotencyConflictError:
        raise
    except _TaskInputValidationError as error:
        error_message = "task input is invalid"
        try:
            await _mark_failed(request_id, error_message)
        except Exception:
            logger.exception("Could not mark MCP request %s as failed", request_id)
        raise MCPRequestValidationError(request_id, error.error) from error
    except Exception as error:
        error_message = _safe_task_error(error)
        try:
            await _mark_failed(request_id, error_message)
        except Exception:
            logger.exception("Could not mark MCP request %s as failed", request_id)
        raise MCPRequestExecutionError(request_id, error_message) from error
