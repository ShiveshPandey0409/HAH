from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from app.core.config import get_settings
from app.mcp.auth import APIKeyAuthMiddleware, get_current_api_client
from app.schemas.submission import SubmissionResponse, VerificationResult
from app.schemas.task import (
    POSTGRES_BIGINT_MAX,
    BountyCreate,
    MCPTaskCreateInput,
    TaskResponse,
)
from app.services.api_clients import (
    SUBMISSIONS_VERIFY_SCOPE,
    TASKS_CREATE_SCOPE,
    require_api_scope,
)
from app.services.mcp_requests import create_task_from_mcp, verify_submission_from_mcp


async def create_task(
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    title: Annotated[str, Field(min_length=1)],
    description: Annotated[str, Field(min_length=1)],
    total_budget_minor: Annotated[int, Field(gt=0, le=POSTGRES_BIGINT_MAX)],
    currency: str,
    bounties: Annotated[list[BountyCreate], Field(min_length=1)],
    deadline_at: datetime | None = None,
) -> TaskResponse:
    principal = get_current_api_client()
    require_api_scope(principal, TASKS_CREATE_SCOPE)
    data = MCPTaskCreateInput(
        title=title,
        description=description,
        total_budget_minor=total_budget_minor,
        currency=currency,
        deadline_at=deadline_at,
        bounties=bounties,
    )
    return await create_task_from_mcp(
        principal,
        idempotency_key=idempotency_key,
        data=data,
    )


async def verify_submission(
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    submission_id: UUID,
    result: VerificationResult,
    checks: dict[str, Any] | None = None,
    failure_reason: str | None = None,
) -> SubmissionResponse:
    principal = get_current_api_client()
    require_api_scope(principal, SUBMISSIONS_VERIFY_SCOPE)
    return await verify_submission_from_mcp(
        principal,
        idempotency_key=idempotency_key,
        submission_id=submission_id,
        result=result,
        checks=checks or {},
        failure_reason=failure_reason,
    )


def create_mcp_server() -> tuple[MCPServer, APIKeyAuthMiddleware]:
    settings = get_settings()
    server = MCPServer(
        "hire-a-human",
        title="Hire a Human",
        description="Create auditable marketing tasks and verify human submissions.",
        version="0.1.0",
    )
    server.tool(
        title="Create marketing task",
        description="Create one draft task and all of its Reddit or LinkedIn bounties.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(create_task)
    server.tool(
        title="Verify task submission",
        description="Verify the latest submission for one of the authenticated creator's tasks.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(verify_submission)
    http_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )
    return server, APIKeyAuthMiddleware(http_app)
