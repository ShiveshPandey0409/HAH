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
from app.models.payment import AuthorizationStatus
from app.models.task import TaskStatus
from app.schemas.payment import (
    GlobalPaymentAllowanceResponse,
    MCPTaskPublishResponse,
    PaymentAuthorizationResponse,
    PaymentResponse,
    WalletResponse,
)
from app.schemas.submission import SubmissionResponse, VerificationResult
from app.schemas.task import (
    POSTGRES_BIGINT_MAX,
    BountyCreate,
    MCPTaskCreateInput,
    TaskResponse,
)
from app.schemas.webhook import (
    CURRENT_WEBHOOK_EVENT_TYPES,
    WebhookEndpointPutResponse,
    WebhookEndpointResponse,
    WebhookEventType,
    WebhookPutRequest,
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
    PaymentNotFoundError,
    get_global_payment_allowance,
    get_payment,
    get_task_payment_authorization,
    get_wallet,
    list_task_payments,
    refresh_task_payment_authorization_and_commit,
    restart_task_payment_authorization_and_commit,
    start_task_payment_authorization_and_commit,
)
from app.services.payments import (
    runtime_from_settings as payment_runtime_from_settings,
)
from app.services.submissions import (
    get_submission,
    get_submission_proof_content,
)
from app.services.submissions import (
    list_task_submissions as list_owned_task_submissions,
)
from app.services.tasks import (
    get_task as get_owned_task,
)
from app.services.tasks import (
    list_tasks as list_owned_tasks,
)
from app.services.tasks import (
    open_task,
)
from app.services.webhooks import configure_webhook_endpoint, get_webhook_endpoint
from app.workers.webhooks import runtime_from_settings as webhook_runtime_from_settings

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


async def list_tasks() -> list[TaskResponse]:
    """List every task owned by the authenticated creator, newest first."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, TASKS_CREATE_SCOPE)
    async with AsyncSessionFactory() as session:
        return await list_owned_tasks(session, principal.user_id)


async def get_task(task_id: UUID) -> TaskResponse:
    """Read one task owned by the authenticated creator."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, TASKS_CREATE_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_owned_task(
            session,
            task_id,
            authorized_creator_id=principal.user_id,
        )


async def publish_task(task_id: UUID) -> MCPTaskPublishResponse:
    """Authorize a task budget and open it, pausing only for required human approval."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, TASKS_CREATE_SCOPE)
    require_api_scope(principal, PAYMENTS_WRITE_SCOPE)
    runtime = payment_runtime_from_settings()

    async with AsyncSessionFactory() as session:
        task = await get_owned_task(
            session,
            task_id,
            authorized_creator_id=principal.user_id,
        )
        try:
            authorization = await get_task_payment_authorization(
                session,
                task_id,
                creator_id=principal.user_id,
            )
        except PaymentNotFoundError:
            authorization = None

    if authorization is None:
        async with AsyncSessionFactory() as session:
            authorization = await start_task_payment_authorization_and_commit(
                session,
                task_id,
                creator_id=principal.user_id,
                runtime=runtime,
            )
    elif authorization.status == AuthorizationStatus.PENDING:
        async with AsyncSessionFactory() as session:
            authorization = await refresh_task_payment_authorization_and_commit(
                session,
                task_id,
                creator_id=principal.user_id,
                runtime=runtime,
            )
    elif authorization.status == AuthorizationStatus.EXPIRED:
        async with AsyncSessionFactory() as session:
            authorization = await restart_task_payment_authorization_and_commit(
                session,
                task_id,
                creator_id=principal.user_id,
                runtime=runtime,
            )

    if authorization.status == AuthorizationStatus.ACTIVE and task.status == TaskStatus.DRAFT:
        async with AsyncSessionFactory() as session:
            task = await open_task(
                session,
                task_id,
                authorized_creator_id=principal.user_id,
            )
    else:
        async with AsyncSessionFactory() as session:
            task = await get_owned_task(
                session,
                task_id,
                authorized_creator_id=principal.user_id,
            )

    if authorization.status == AuthorizationStatus.ACTIVE and task.status == TaskStatus.OPEN:
        next_action = "task_open"
    elif authorization.approval_url is not None:
        next_action = "open_approval_url_then_call_publish_task_again"
    elif authorization.status == AuthorizationStatus.PENDING:
        next_action = "finish_human_approval_then_call_publish_task_again"
    else:
        next_action = "payment_authorization_not_active"

    return MCPTaskPublishResponse(
        task=task,
        payment_authorization=authorization,
        ready=next_action == "task_open",
        human_approval_required=next_action
        in {
            "open_approval_url_then_call_publish_task_again",
            "finish_human_approval_then_call_publish_task_again",
        },
        next_action=next_action,
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


async def list_task_submissions(task_id: UUID) -> list[SubmissionResponse]:
    """List all human submissions for one owned task, newest first."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, SUBMISSIONS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await list_owned_task_submissions(
            session,
            task_id,
            authorized_creator_id=principal.user_id,
        )


async def start_task_payment_authorization(
    task_id: UUID,
) -> PaymentAuthorizationResponse:
    """Reserve a task from the approved HAH allowance, requesting a top-up only if needed."""

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
    """Activate a pending global HAH allowance after its one-time human approval."""

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
    """Replace the pending global allowance's consumed or expired approval session."""

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


async def get_global_allowance(
    currency: str = "USD",
) -> GlobalPaymentAllowanceResponse:
    """Read approved, allocated, available, and pending HAH allowance amounts."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_global_payment_allowance(session, currency=currency)


async def get_payment_status(payment_id: UUID) -> PaymentResponse:
    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_payment(
            session,
            payment_id,
            authorized_user_id=principal.user_id,
        )


async def get_task_payment_authorization_status(
    task_id: UUID,
) -> PaymentAuthorizationResponse:
    """Read the payment authorization for one owned task."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_task_payment_authorization(
            session,
            task_id,
            creator_id=principal.user_id,
        )


async def list_task_payment_statuses(task_id: UUID) -> list[PaymentResponse]:
    """List every automatic reward payment for one owned task."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await list_task_payments(
            session,
            task_id,
            creator_id=principal.user_id,
        )


async def get_wallet_balance() -> WalletResponse:
    principal = get_current_oauth_principal()
    require_api_scope(principal, PAYMENTS_READ_SCOPE)
    async with AsyncSessionFactory() as session:
        return await get_wallet(session, user_id=principal.user_id)


async def configure_webhook(
    url: Annotated[str, Field(min_length=1, max_length=2048)],
    subscribed_events: list[WebhookEventType] | None = None,
) -> WebhookEndpointPutResponse:
    """Create or rotate the authenticated creator's signed webhook endpoint."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, SUBMISSIONS_APPROVE_SCOPE)
    runtime = webhook_runtime_from_settings()
    data = WebhookPutRequest(
        url=url,
        subscribed_events=(
            subscribed_events
            if subscribed_events is not None
            else sorted(CURRENT_WEBHOOK_EVENT_TYPES, key=lambda event: event.value)
        ),
    )
    async with AsyncSessionFactory() as session:
        return await configure_webhook_endpoint(
            session,
            creator_id=principal.user_id,
            data=data,
            cipher=runtime.cipher,
            resolver=runtime.resolver,
            policy=runtime.policy,
        )


async def get_webhook() -> WebhookEndpointResponse:
    """Read the authenticated creator's webhook URL and subscribed events."""

    principal = get_current_oauth_principal()
    require_api_scope(principal, SUBMISSIONS_READ_SCOPE)
    runtime = webhook_runtime_from_settings()
    async with AsyncSessionFactory() as session:
        return await get_webhook_endpoint(
            session,
            creator_id=principal.user_id,
            cipher=runtime.cipher,
            policy=runtime.policy,
        )


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
        description=(
            "Create, fund, and monitor auditable human marketing tasks. After create_task, "
            "call publish_task. It returns a human approval URL only when the reusable "
            "allowance needs funding; call publish_task again after approval. Later tasks "
            "normally publish immediately from the same allowance. Use webhooks or the list "
            "tools to monitor submissions, verification, and payments."
        ),
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
        title="List owned tasks",
        description="List every task owned by the authenticated creator, newest first.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(list_tasks)
    server.tool(
        title="Get task",
        description="Read one owned task, its current status, and its bounty progress.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_task)
    server.tool(
        title="Fund and publish task",
        description=(
            "Call immediately after create_task. The first call returns one human approval "
            "URL only if the reusable HAH allowance needs funding. Call again after approval; "
            "later tasks usually fund and open in one call."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )(publish_task)
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
        title="List task submissions",
        description=(
            "List every human submission and verification state for one owned task. "
            "Use get_submission_proofs when image content is needed."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(list_task_submissions)
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
            "Atomically reserve one owned task from the reusable HAH allowance. Returns "
            "a Prava approval URL only when the shared allowance needs a top-up."
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
            "Check Prava after the one-time global approval and activate every task "
            "reservation backed by that allowance pool."
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
            "Replace a consumed or expired global top-up session and return a fresh URL. "
            "This is never needed while an approved allowance has enough capacity."
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
        title="Get global payment allowance",
        description=(
            "Read the HAH-wide approved, allocated, available, and pending Prava allowance "
            "without exposing card or mandate credentials."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_global_allowance)
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
        title="Get task payment authorization",
        description="Read the allowance reservation and human-approval state for one owned task.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_task_payment_authorization_status)
    server.tool(
        title="List task payments",
        description="List every automatic human reward payment for one owned task.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(list_task_payment_statuses)
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
    server.tool(
        title="Configure signed webhook",
        description=(
            "Create or rotate the authenticated creator's HTTPS webhook. Returns the signing "
            "secret once; store it securely. Omitting events subscribes to all supported events."
        ),
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )(configure_webhook)
    server.tool(
        title="Get webhook configuration",
        description="Read the configured webhook URL, subscriptions, and delivery policy.",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )(get_webhook)
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
