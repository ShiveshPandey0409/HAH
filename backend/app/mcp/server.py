from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings, get_settings
from app.db.session import AsyncSessionFactory
from app.mcp.oauth import (
    MCP_ACCESS_SCOPE,
    build_oauth_token_verifier,
    get_current_oauth_principal,
)
from app.schemas.payment import PaymentAuthorizationResponse, PaymentResponse, WalletResponse
from app.schemas.submission import SubmissionResponse, VerificationResult
from app.schemas.task import (
    POSTGRES_BIGINT_MAX,
    BountyCreate,
    MCPTaskCreateInput,
    TaskResponse,
)
from app.services.api_clients import (
    PAYMENTS_READ_SCOPE,
    PAYMENTS_WRITE_SCOPE,
    SUBMISSIONS_APPROVE_SCOPE,
    SUBMISSIONS_READ_SCOPE,
    SUBMISSIONS_VERIFY_SCOPE,
    TASKS_CREATE_SCOPE,
    require_api_scope,
)
from app.services.mcp_requests import create_task_from_mcp, verify_submission_from_mcp
from app.services.payments import (
    get_payment,
    get_wallet,
    refresh_task_payment_authorization_and_commit,
    restart_task_payment_authorization_and_commit,
    start_task_payment_authorization_and_commit,
)
from app.services.payments import (
    runtime_from_settings as payment_runtime_from_settings,
)
from app.services.submissions import get_submission, get_submission_proof_content

MCP_SUPPORTED_SCOPES = (
    MCP_ACCESS_SCOPE,
    TASKS_CREATE_SCOPE,
    SUBMISSIONS_READ_SCOPE,
    SUBMISSIONS_VERIFY_SCOPE,
    SUBMISSIONS_APPROVE_SCOPE,
    PAYMENTS_READ_SCOPE,
    PAYMENTS_WRITE_SCOPE,
)
OAUTH_PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


async def create_task(
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    title: Annotated[str, Field(min_length=1)],
    description: Annotated[str, Field(min_length=1)],
    total_budget_minor: Annotated[int, Field(gt=0, le=POSTGRES_BIGINT_MAX)],
    currency: str,
    bounties: Annotated[list[BountyCreate], Field(min_length=1)],
    deadline_at: datetime | None = None,
) -> TaskResponse:
    principal = get_current_oauth_principal()
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
    principal = get_current_oauth_principal()
    require_api_scope(principal, SUBMISSIONS_VERIFY_SCOPE)
    return await verify_submission_from_mcp(
        principal,
        idempotency_key=idempotency_key,
        submission_id=submission_id,
        result=result,
        checks=checks or {},
        failure_reason=failure_reason,
    )


async def get_submission_proofs(submission_id: UUID) -> CallToolResult:
    """Read one owned submission and return its image proofs as MCP image content."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, SUBMISSIONS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        submission = await get_submission(
            session,
            submission_id,
            authorized_user_id=principal.user_id,
            creator_only=True,
        )
        content = [
            TextContent(
                type="text",
                text=json.dumps(
                    submission.model_dump(mode="json"),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        ]
        for proof in submission.proofs:
            if proof.upload_id is None:
                continue
            image = await get_submission_proof_content(
                session,
                submission.id,
                proof.id,
                authorized_user_id=principal.user_id,
                creator_only=True,
            )
            content.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(image.content).decode("ascii"),
                    mime_type=image.mime_type,
                )
            )
    return CallToolResult(
        content=content,
        structured_content=submission.model_dump(mode="json"),
    )


async def start_task_payment_authorization(
    task_id: UUID,
) -> PaymentAuthorizationResponse:
    """Start the one-time human approval needed for automatic task rewards."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_WRITE_SCOPE)
    runtime = payment_runtime_from_settings()
    async with AsyncSessionFactory() as session:
        return await start_task_payment_authorization_and_commit(
            session,
            task_id,
            creator_id=principal.user_id,
            runtime=runtime,
        )


async def refresh_task_payment_authorization(
    task_id: UUID,
) -> PaymentAuthorizationResponse:
    """Refresh a task mandate after the universal payer opens its approval URL."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_WRITE_SCOPE)
    runtime = payment_runtime_from_settings()
    async with AsyncSessionFactory() as session:
        return await refresh_task_payment_authorization_and_commit(
            session,
            task_id,
            creator_id=principal.user_id,
            runtime=runtime,
        )


async def restart_task_payment_authorization(
    task_id: UUID,
) -> PaymentAuthorizationResponse:
    """Replace a consumed or failed hosted approval session for one owned task."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_WRITE_SCOPE)
    runtime = payment_runtime_from_settings()
    async with AsyncSessionFactory() as session:
        return await restart_task_payment_authorization_and_commit(
            session,
            task_id,
            creator_id=principal.user_id,
            runtime=runtime,
        )


async def get_payment_status(payment_id: UUID) -> PaymentResponse:
    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_payment(
            session,
            payment_id,
            authorized_user_id=principal.user_id,
        )


async def get_wallet_balance() -> WalletResponse:
    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_wallet(session, user_id=principal.user_id)


class OAuthChallengeScopeMiddleware:
    """Publish complete OAuth metadata and normalize MCP SDK 2.0 challenges."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.metadata = {
            "resource": str(settings.mcp_public_url),
            "authorization_servers": [str(settings.mcp_oauth_issuer_url)],
            "scopes_supported": list(MCP_SUPPORTED_SCOPES),
            "bearer_methods_supported": ["header"],
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and scope["path"] == OAUTH_PROTECTED_RESOURCE_METADATA_PATH
        ):
            response = JSONResponse(
                self.metadata,
                headers={"Access-Control-Allow-Origin": "*"},
            )
            await response(scope, receive, send)
            return

        async def send_with_scope(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in {401, 403}:
                headers = list(message.get("headers", []))
                for index, (name, value) in enumerate(headers):
                    if name.lower() != b"www-authenticate" or not value.lower().startswith(
                        b"bearer"
                    ):
                        continue
                    if b"scope=" not in value.lower():
                        value += f', scope="{MCP_ACCESS_SCOPE}"'.encode()
                        headers[index] = (name, value)
                    break
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_scope)


def create_mcp_server(
    *,
    app_settings: Settings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> tuple[MCPServer, ASGIApp]:
    settings = app_settings or get_settings()
    verifier = token_verifier or build_oauth_token_verifier(settings)
    server = MCPServer(
        "hire-a-human",
        title="Hire a Human",
        description="Create auditable marketing tasks and verify human submissions.",
        version="0.1.0",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.mcp_oauth_issuer_url,
            resource_server_url=settings.mcp_public_url,
            required_scopes=[MCP_ACCESS_SCOPE],
        ),
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
        title="Get submission proofs",
        description=(
            "Read one submission for the authenticated creator and return uploaded proofs, "
            "including screenshots as MCP image content."
        ),
        structured_output=False,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_submission_proofs)
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
    server.tool(
        title="Start task payment authorization",
        description=(
            "Allocate one owned task's exact budget. Returns the task and other-task "
            "blocked amounts plus a Prava approval URL when more approval is required."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )(start_task_payment_authorization)
    server.tool(
        title="Refresh task payment authorization",
        description=(
            "Check Prava after human approval and return blocked, used, and remaining "
            "task-budget amounts for automatic rewards."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )(refresh_task_payment_authorization)
    server.tool(
        title="Restart task payment authorization",
        description=(
            "Replace a consumed or failed Prava approval session and return a fresh URL. "
            "Open the returned URL once in a passkey-capable browser."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )(restart_task_payment_authorization)
    server.tool(
        title="Get payment status",
        description="Read one HAH reward payment without exposing payment credentials.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_payment_status)
    server.tool(
        title="Get internal wallet balance",
        description="Read the authenticated user's non-redeemable hackathon reward wallet.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_wallet_balance)
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
    return server, OAuthChallengeScopeMiddleware(http_app, settings=settings)
