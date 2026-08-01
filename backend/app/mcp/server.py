from __future__ import annotations

from datetime import datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from app.core.config import get_settings
from app.mcp.auth import APIKeyAuthMiddleware, get_current_api_client
from app.schemas.task import (
    POSTGRES_BIGINT_MAX,
    BountyCreate,
    MCPTaskCreateInput,
    TaskResponse,
)
from app.services.api_clients import TASKS_CREATE_SCOPE, require_api_scope
from app.services.mcp_requests import create_task_from_mcp


async def create_task(
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    title: Annotated[str, Field(min_length=1)],
    description: Annotated[str, Field(min_length=1)],
    total_budget_minor: Annotated[int, Field(gt=0, le=POSTGRES_BIGINT_MAX)],
    currency: str,
    bounties: Annotated[list[BountyCreate], Field(min_length=1)],
    ctx: Context,
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


def create_mcp_server() -> tuple[MCPServer, APIKeyAuthMiddleware]:
    settings = get_settings()
    server = MCPServer(
        "hire-a-human",
        title="Hire a Human",
        description="Create auditable human marketing tasks and bounties.",
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
