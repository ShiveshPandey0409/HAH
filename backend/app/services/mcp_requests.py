from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import REDACTED, canonical_json_fingerprint, redact_sensitive_data
from app.db.session import AsyncSessionFactory
from app.mcp.oauth import MCP_ACCESS_SCOPE, OAuthPrincipal
from app.models.claim import BountyClaim
from app.models.integration import MCPRequest, RequestStatus
from app.models.submission import Submission, VerificationMethod
from app.models.task import Bounty, Task
from app.schemas.submission import (
    SubmissionResponse,
    VerificationCommand,
    VerificationResult,
)
from app.schemas.task import MCPTaskCreateInput, TaskCreate, TaskResponse
from app.schemas.webhook import (
    MCPRequestCompletedData,
    WebhookEventType,
)
from app.services.api_clients import (
    SUBMISSIONS_APPROVE_SCOPE,
    SUBMISSIONS_VERIFY_SCOPE,
    TASKS_CREATE_SCOPE,
    APIClientPrincipal,
    require_api_scope,
)
from app.services.submissions import (
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionValidationError,
    verify_submission,
)
from app.services.tasks import (
    CreatorCannotCreateTasksError,
    CreatorNotFoundError,
    TaskValidationError,
    create_task,
    get_task,
)
from app.services.webhooks import enqueue_webhook_event

CREATE_TASK_METHOD = "create_task"
VERIFY_SUBMISSION_METHOD = "verify_submission"
MCPMethod = Literal["create_task", "verify_submission"]
logger = logging.getLogger(__name__)


type MCPPrincipal = APIClientPrincipal | OAuthPrincipal


class IdempotencyConflictError(Exception):
    pass


def _require_tool_scope(principal: MCPPrincipal, scope: str) -> None:
    if isinstance(principal, OAuthPrincipal):
        require_api_scope(principal, MCP_ACCESS_SCOPE)
    require_api_scope(principal, scope)


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


class _SubmissionInputValidationError(Exception):
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
    principal: MCPPrincipal,
    method: str,
    idempotency_key: str,
    request_data: dict[str, Any],
) -> UUID:
    auth_scopes = sorted(principal.scopes)
    values: dict[str, Any] = {
        "id": new_request_id,
        "actor_user_id": principal.creator_id,
        "auth_scopes": auth_scopes,
        "method": method,
        "idempotency_key": idempotency_key,
        "status": RequestStatus.STARTED,
        "request_data": request_data,
    }
    if isinstance(principal, APIClientPrincipal):
        values.update(
            api_client_id=principal.client_id,
            oauth_delegation_id=None,
            oauth_consent_version=None,
            oauth_authorization_id=None,
        )
        conflict_columns = [MCPRequest.api_client_id, MCPRequest.idempotency_key]
        conflict_where = None
        existing_request = (
            MCPRequest.api_client_id == principal.client_id,
            MCPRequest.idempotency_key == idempotency_key,
        )
    else:
        values.update(
            api_client_id=None,
            oauth_delegation_id=principal.delegation_id,
            oauth_consent_version=principal.consent_version,
            oauth_authorization_id=principal.authorization_id,
        )
        conflict_columns = [MCPRequest.oauth_delegation_id, MCPRequest.idempotency_key]
        conflict_where = MCPRequest.oauth_delegation_id.is_not(None)
        existing_request = (
            MCPRequest.oauth_delegation_id == principal.delegation_id,
            MCPRequest.idempotency_key == idempotency_key,
        )

    async with AsyncSessionFactory() as session:
        reservation = (
            insert(MCPRequest)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=conflict_columns,
                index_where=conflict_where,
            )
        )
        request_id = await session.scalar(reservation.returning(MCPRequest.id))
        if request_id is None:
            request_id = await session.scalar(select(MCPRequest.id).where(*existing_request))
        await session.commit()

    if request_id is None:
        raise RuntimeError("MCP request reservation could not be loaded")
    return request_id


def _authorization_snapshot_matches(request: MCPRequest, principal: MCPPrincipal) -> bool:
    if request.actor_user_id != principal.creator_id:
        return False
    if isinstance(principal, APIClientPrincipal):
        return (
            request.api_client_id == principal.client_id
            and request.oauth_delegation_id is None
            and request.oauth_consent_version is None
            and request.oauth_authorization_id is None
            and request.auth_scopes == sorted(principal.scopes)
        )
    return (
        request.api_client_id is None
        and request.oauth_delegation_id == principal.delegation_id
        and request.oauth_consent_version == principal.consent_version
        and request.oauth_authorization_id == principal.authorization_id
    )


def _require_retry_authorization_snapshot(
    request: MCPRequest,
    principal: MCPPrincipal,
) -> None:
    if not _authorization_snapshot_matches(request, principal):
        raise IdempotencyConflictError(
            "idempotency key belongs to a different authorization snapshot"
        )


def _task_request_data(
    principal: MCPPrincipal,
    data: MCPTaskCreateInput,
) -> dict[str, Any]:
    raw_input = {
        "creator_id": str(principal.creator_id),
        **data.model_dump(mode="json"),
    }
    return {
        "creator_id": str(principal.creator_id),
        "currency": data.currency,
        "total_budget_minor": data.total_budget_minor,
        "bounty_count": len(data.bounties),
        "deadline_present": data.deadline_at is not None,
        "input_fingerprint": canonical_json_fingerprint(redact_sensitive_data(raw_input)),
    }


def _task_response_audit(response: TaskResponse) -> dict[str, Any]:
    return {
        "task_id": str(response.id),
        "status": "created",
        "created_via": response.created_via,
        "bounty_count": len(response.bounties),
        "output_fingerprint": canonical_json_fingerprint(response.model_dump(mode="json")),
    }


async def _replay_task_creation_response(
    session: AsyncSession,
    request: MCPRequest,
    *,
    creator_id: UUID,
) -> TaskResponse:
    if request.task_id is None or not isinstance(request.response_data, dict):
        raise RuntimeError("stored MCP task result is invalid")
    response = await get_task(session, request.task_id)
    if response.creator_id != creator_id:
        raise RuntimeError("stored MCP task owner is invalid")

    original = response.model_dump()
    original["status"] = "draft"
    original["updated_at"] = original["created_at"]
    for bounty in original["bounties"]:
        bounty["status"] = "draft"
        bounty["claim_count"] = 0
        bounty["remaining_slots"] = bounty["slot_count"]
        bounty["updated_at"] = bounty["created_at"]
    replay = TaskResponse.model_validate(original)
    expected = request.response_data.get("output_fingerprint")
    if expected != canonical_json_fingerprint(replay.model_dump(mode="json")):
        raise RuntimeError("stored MCP task result no longer matches its resource")
    return replay


def _safe_verification_response(response: SubmissionResponse) -> SubmissionResponse:
    safe = response.model_dump()
    safe["proofs"] = [
        {
            **proof,
            "url": None,
            "storage_key": None,
            "sha256": None,
        }
        for proof in safe["proofs"]
    ]
    safe["checks"] = redact_sensitive_data(safe["checks"])
    safe["failure_reason"] = REDACTED if safe["failure_reason"] is not None else None
    return SubmissionResponse.model_validate(safe)


def _task_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, CreatorNotFoundError):
        return "task creator no longer exists", "task_creator_not_found"
    if isinstance(error, CreatorCannotCreateTasksError):
        return "delegated user cannot create tasks", "task_creator_ineligible"
    if isinstance(error, TaskValidationError):
        return "task or bounty violates a database rule", "task_validation_failed"
    return "task creation failed", "task_creation_failed"


def _submission_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, SubmissionNotFoundError):
        return "submission not found", "submission_not_found"
    if isinstance(error, SubmissionConflictError):
        return "submission verification conflicts with current state", "submission_conflict"
    if isinstance(error, SubmissionValidationError):
        return "submission verification is invalid", "submission_validation_failed"
    return "submission verification failed", "submission_verification_failed"


async def _enqueue_mcp_request_completed(
    session: AsyncSession,
    *,
    request: MCPRequest,
    creator_id: UUID,
    error_code: str | None = None,
) -> None:
    if request.method not in {CREATE_TASK_METHOD, VERIFY_SUBMISSION_METHOD}:
        return
    if request.completed_at is None:
        raise RuntimeError("completed MCP request is missing its completion timestamp")

    method = cast(MCPMethod, request.method)
    succeeded = request.status == RequestStatus.SUCCEEDED
    status: Literal["succeeded", "failed"] = "succeeded" if succeeded else "failed"
    data = MCPRequestCompletedData(
        request_id=request.id,
        method=method,
        status=status,
        completed_at=request.completed_at,
        task_id=request.task_id if method == CREATE_TASK_METHOD else None,
        submission_id=(request.submission_id if method == VERIFY_SUBMISSION_METHOD else None),
        error_code=None if succeeded else error_code or "mcp_request_failed",
    )
    await enqueue_webhook_event(
        session,
        creator_id=creator_id,
        event_type=WebhookEventType.MCP_REQUEST_COMPLETED,
        entity_type="mcp_request",
        entity_id=request.id,
        data=data,
        deduplication_key=f"mcp_request.completed:{request.id}:{status}",
        created_at=request.completed_at,
    )


async def _mark_failed(
    request_id: UUID,
    error_message: str,
    *,
    error_code: str = "mcp_request_failed",
    submission_id: UUID | None = None,
) -> None:
    async with AsyncSessionFactory() as session:
        request = await session.scalar(
            select(MCPRequest).where(MCPRequest.id == request_id).with_for_update()
        )
        if request is None or request.status == RequestStatus.SUCCEEDED:
            return

        request.status = RequestStatus.FAILED
        request.response_data = None
        request.error_message = error_message
        request.completed_at = datetime.now(UTC)
        creator_id = request.actor_user_id
        if submission_id is not None and request.method == VERIFY_SUBMISSION_METHOD:
            owned_submission_id = await session.scalar(
                select(Submission.id)
                .join(BountyClaim, BountyClaim.id == Submission.claim_id)
                .join(Bounty, Bounty.id == BountyClaim.bounty_id)
                .join(Task, Task.id == Bounty.task_id)
                .where(
                    Submission.id == submission_id,
                    Task.creator_id == creator_id,
                )
            )
            request.submission_id = owned_submission_id
        try:
            async with session.begin_nested():
                await _enqueue_mcp_request_completed(
                    session,
                    request=request,
                    creator_id=creator_id,
                    error_code=error_code,
                )
        except Exception:
            logger.warning(
                "Could not enqueue failed MCP request event for %s",
                request_id,
            )
        await session.commit()


async def create_task_from_mcp(
    principal: MCPPrincipal,
    *,
    idempotency_key: str,
    data: MCPTaskCreateInput,
) -> TaskResponse:
    _require_tool_scope(principal, TASKS_CREATE_SCOPE)
    normalized_key = _normalize_idempotency_key(idempotency_key)
    request_data = _task_request_data(principal, data)
    request_id = uuid4()

    try:
        request_id = await _reserve_request(
            new_request_id=request_id,
            principal=principal,
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
                return await _replay_task_creation_response(
                    session,
                    request,
                    creator_id=principal.creator_id,
                )
            _require_retry_authorization_snapshot(request, principal)

            request.status = RequestStatus.STARTED
            request.response_data = None
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
            request.response_data = _task_response_audit(response)
            request.error_message = None
            request.task_id = response.id
            request.completed_at = datetime.now(UTC)
            await _enqueue_mcp_request_completed(
                session,
                request=request,
                creator_id=principal.creator_id,
            )
            await session.commit()
            return response
    except IdempotencyConflictError:
        raise
    except _TaskInputValidationError as error:
        error_message = "task input is invalid"
        try:
            await _mark_failed(
                request_id,
                error_message,
                error_code="task_input_invalid",
            )
        except Exception:
            logger.exception("Could not mark MCP request %s as failed", request_id)
        raise MCPRequestValidationError(request_id, error.error) from error
    except Exception as error:
        error_message, error_code = _task_error(error)
        try:
            await _mark_failed(
                request_id,
                error_message,
                error_code=error_code,
            )
        except Exception:
            logger.exception("Could not mark MCP request %s as failed", request_id)
        raise MCPRequestExecutionError(request_id, error_message) from error


def _verification_request_data(
    principal: MCPPrincipal,
    *,
    submission_id: UUID,
    result: VerificationResult,
    checks: dict[str, Any],
    failure_reason: str | None,
) -> dict[str, Any]:
    raw_input = {
        "submission_id": str(submission_id),
        "result": result,
        "checks": checks,
        "failure_reason": failure_reason,
    }
    return {
        "creator_id": str(principal.creator_id),
        "submission_id": str(submission_id),
        "result": result,
        "check_count": len(checks),
        # The business record retains the reason. The MCP audit only records its
        # presence so free-form text cannot create a second copy of a secret.
        "failure_reason_present": failure_reason is not None,
        "input_fingerprint": canonical_json_fingerprint(redact_sensitive_data(raw_input)),
    }


async def verify_submission_from_mcp(
    principal: MCPPrincipal,
    *,
    idempotency_key: str,
    submission_id: UUID,
    result: VerificationResult,
    checks: dict[str, Any],
    failure_reason: str | None = None,
) -> SubmissionResponse:
    _require_tool_scope(principal, SUBMISSIONS_VERIFY_SCOPE)
    if result == "passed":
        require_api_scope(principal, SUBMISSIONS_APPROVE_SCOPE)
    normalized_key = _normalize_idempotency_key(idempotency_key)
    request_data = _verification_request_data(
        principal,
        submission_id=submission_id,
        result=result,
        checks=checks,
        failure_reason=failure_reason,
    )
    request_id = uuid4()

    try:
        request_id = await _reserve_request(
            new_request_id=request_id,
            principal=principal,
            method=VERIFY_SUBMISSION_METHOD,
            idempotency_key=normalized_key,
            request_data=request_data,
        )
        async with AsyncSessionFactory() as session:
            request = await session.scalar(
                select(MCPRequest).where(MCPRequest.id == request_id).with_for_update()
            )
            if request is None:
                raise RuntimeError("MCP request reservation disappeared")
            if request.method != VERIFY_SUBMISSION_METHOD or request.request_data != request_data:
                raise IdempotencyConflictError(
                    "idempotency key was already used with different input"
                )
            if request.status == RequestStatus.SUCCEEDED:
                if not isinstance(request.response_data, dict):
                    raise RuntimeError("stored MCP response is invalid")
                return SubmissionResponse.model_validate(request.response_data)
            _require_retry_authorization_snapshot(request, principal)

            request.status = RequestStatus.STARTED
            request.response_data = None
            request.error_message = None
            request.completed_at = None
            try:
                command = VerificationCommand(
                    result=result,
                    checks=checks,
                    failure_reason=failure_reason,
                )
            except ValidationError as error:
                raise _SubmissionInputValidationError(error) from error

            response = await verify_submission(
                session,
                submission_id,
                command,
                method=VerificationMethod.MCP,
                verifier_user_id=principal.creator_id,
                authorized_creator_id=principal.creator_id,
            )
            response = _safe_verification_response(response)
            request.status = RequestStatus.SUCCEEDED
            request.response_data = response.model_dump(mode="json")
            request.error_message = None
            request.submission_id = response.id
            request.completed_at = datetime.now(UTC)
            await _enqueue_mcp_request_completed(
                session,
                request=request,
                creator_id=principal.creator_id,
            )
            await session.commit()
            return response
    except IdempotencyConflictError:
        raise
    except _SubmissionInputValidationError as error:
        error_message = "submission verification input is invalid"
        try:
            await _mark_failed(
                request_id,
                error_message,
                error_code="verification_input_invalid",
                submission_id=submission_id,
            )
        except Exception:
            logger.exception("Could not mark MCP request %s as failed", request_id)
        raise MCPRequestValidationError(request_id, error.error) from error
    except Exception as error:
        error_message, error_code = _submission_error(error)
        try:
            await _mark_failed(
                request_id,
                error_message,
                error_code=error_code,
                submission_id=submission_id,
            )
        except Exception:
            logger.exception("Could not mark MCP request %s as failed", request_id)
        raise MCPRequestExecutionError(request_id, error_message) from error
