from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from mcp import Client
from mcp.server.auth.provider import AccessToken
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.main import app
from app.mcp.oauth import MCP_ACCESS_SCOPE, OAuthPrincipal, use_oauth_principal
from app.mcp.server import create_mcp_server
from app.models.integration import (
    APIClient,
    IntegrationStatus,
    MCPRequest,
    OAuthDelegation,
    OAuthIdentity,
    RequestStatus,
)
from app.models.task import Bounty, Task
from app.schemas.task import POSTGRES_BIGINT_MAX, MCPTaskCreateInput
from app.services import mcp_requests
from app.services.api_clients import (
    PAYMENTS_READ_SCOPE,
    PAYMENTS_WRITE_SCOPE,
    SUBMISSIONS_APPROVE_SCOPE,
    SUBMISSIONS_READ_SCOPE,
    SUBMISSIONS_VERIFY_SCOPE,
    TASKS_CREATE_SCOPE,
    APIClientPrincipal,
    InvalidAPIKeyError,
    authenticate_api_token,
    issue_api_client,
)
from app.services.mcp_requests import (
    IdempotencyConflictError,
    MCPRequestExecutionError,
    _mark_failed,
    create_task_from_mcp,
)
from app.services.oauth_delegations import grant_oauth_delegation
from tests.test_tasks import bounty_payload, create_user, task_payload


def mcp_arguments(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "idempotency_key": "task-create-001",
        "title": "Launch campaign",
        "description": "Promote the product launch.",
        "total_budget_minor": 5_000,
        "currency": "usd",
        "deadline_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        "bounties": [bounty_payload()],
    }
    overrides = dict(overrides)
    if "bounties" in overrides:
        arguments["bounties"] = overrides.pop("bounties")
    arguments.update(overrides)
    return arguments


def task_command(arguments: dict[str, object]) -> MCPTaskCreateInput:
    data = {key: value for key, value in arguments.items() if key != "idempotency_key"}
    return MCPTaskCreateInput(**data)


async def issue_client(
    creator_id: UUID,
    *,
    scopes: set[str] | None = None,
) -> tuple[str, OAuthPrincipal]:
    settings = get_settings()
    oauth_client_id = f"test-agent-{uuid4()}"
    subject = f"test-subject-{uuid4()}"
    approved_scopes = {MCP_ACCESS_SCOPE, *(scopes if scopes is not None else {TASKS_CREATE_SCOPE})}
    authorization_id = f"test-authorization-{uuid4()}"
    async with AsyncSessionFactory() as session:
        identity = OAuthIdentity(
            user_id=creator_id,
            issuer=str(settings.mcp_oauth_issuer_url),
            subject=subject,
            status=IntegrationStatus.ACTIVE,
        )
        session.add(identity)
        await session.flush()
        delegation = OAuthDelegation(
            identity_id=identity.id,
            oauth_client_id=oauth_client_id,
            approved_scopes=sorted(approved_scopes),
            status=IntegrationStatus.ACTIVE,
            consent_version=1,
            authorization_id=authorization_id,
        )
        session.add(delegation)
        await session.flush()
        principal = OAuthPrincipal(
            identity_id=identity.id,
            delegation_id=delegation.id,
            user_id=creator_id,
            client_id=oauth_client_id,
            issuer=identity.issuer,
            subject=subject,
            authorization_id=authorization_id,
            scopes=frozenset(approved_scopes),
            consent_version=1,
        )
        await session.commit()
    return f"oauth-test-token-{uuid4()}", principal


async def issue_legacy_client(creator_id: UUID) -> tuple[str, APIClientPrincipal]:
    async with AsyncSessionFactory() as session:
        issued = await issue_api_client(
            session,
            creator_id=creator_id,
            name="Legacy test agent",
            scopes={TASKS_CREATE_SCOPE},
        )
    return issued.token, APIClientPrincipal(
        client_id=issued.client.id,
        creator_id=creator_id,
        scopes=frozenset(issued.client.scopes),
    )


async def call_create_tool(
    principal: OAuthPrincipal,
    arguments: dict[str, object],
):
    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            return await mcp_client.call_tool("create_task", arguments)


async def test_api_key_is_hashed_authenticated_and_disabled(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    token, principal = await issue_legacy_client(creator_id)
    secret = token.rsplit(".", 1)[1]

    async with AsyncSessionFactory() as session:
        stored = await session.get(APIClient, principal.client_id)
        assert stored is not None
        assert stored.secret_hash.startswith("sha256:")
        assert token not in stored.secret_hash
        assert secret not in stored.secret_hash
        assert stored.last_used_at is None

    authenticated = await authenticate_api_token(token)
    assert authenticated == principal
    for invalid_token in ("malformed", f"{token}wrong", "hah.unknown.secret"):
        with pytest.raises(InvalidAPIKeyError):
            await authenticate_api_token(invalid_token)

    async with AsyncSessionFactory() as session:
        stored = await session.get(APIClient, principal.client_id)
        assert stored is not None
        assert stored.last_used_at is not None
        first_used_at = stored.last_used_at

    assert await authenticate_api_token(token) == principal
    async with AsyncSessionFactory() as session:
        stored = await session.get(APIClient, principal.client_id)
        assert stored is not None
        assert stored.last_used_at == first_used_at
        stored.last_used_at = datetime.now(UTC) - timedelta(minutes=6)
        await session.commit()

    assert await authenticate_api_token(token) == principal
    async with AsyncSessionFactory() as session:
        stored = await session.get(APIClient, principal.client_id)
        assert stored is not None
        assert stored.last_used_at > first_used_at
        stored.status = IntegrationStatus.DISABLED
        await session.commit()

    with pytest.raises(InvalidAPIKeyError):
        await authenticate_api_token(token)


async def test_authenticated_mcp_http_transport_and_host_protection(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    token, principal = await issue_client(creator_id)

    class StaticTokenVerifier:
        async def verify_token(self, candidate: str) -> AccessToken | None:
            if candidate != token:
                return None
            return AccessToken(
                token=candidate,
                client_id=principal.client_id,
                scopes=sorted(principal.scopes),
                expires_at=int(datetime.now(UTC).timestamp()) + 600,
                resource=str(get_settings().mcp_public_url),
                subject=principal.subject,
                claims={
                    "iss": principal.issuer,
                    "oauth_identity_id": str(principal.identity_id),
                    "oauth_delegation_id": str(principal.delegation_id),
                    "oauth_authorization_id": principal.authorization_id,
                    "user_id": str(principal.user_id),
                    "oauth_consent_version": principal.consent_version,
                },
            )

    mcp_server, mcp_http_app = create_mcp_server(token_verifier=StaticTokenVerifier())
    transport = ASGITransport(app=mcp_http_app)

    protocol_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    async with mcp_server.session_manager.run():
        async with AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:9999",
        ) as mcp_http:
            metadata_path = "/.well-known/oauth-protected-resource/mcp"
            metadata = await mcp_http.get(
                metadata_path,
                headers={"Origin": "https://agent.example"},
            )
            metadata_options = await mcp_http.options(
                metadata_path,
                headers={
                    "Origin": "https://agent.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            missing = await mcp_http.post("/mcp", json={})
            invalid = await mcp_http.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}wrong"},
                json={},
            )
            query_token = await mcp_http.post(f"/mcp?access_token={token}", json={})
            legacy_key = await mcp_http.post(
                "/mcp",
                headers={"Authorization": "Bearer hah.legacy.secret"},
                json={},
            )
            initialized = await mcp_http.post(
                "/mcp",
                headers=protocol_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1"},
                    },
                },
            )
            listed = await mcp_http.post(
                "/mcp",
                headers=protocol_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            called = await mcp_http.post(
                "/mcp",
                headers=protocol_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "create_task", "arguments": mcp_arguments()},
                },
            )
        async with AsyncClient(
            transport=transport,
            base_url="http://attacker.example",
        ) as hostile:
            rebinding = await hostile.post(
                "/mcp",
                headers=protocol_headers,
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            )

    assert metadata.status_code == 200
    assert metadata.headers["access-control-allow-origin"] == "*"
    assert metadata.json() == {
        "resource": str(get_settings().mcp_public_url),
        "authorization_servers": [str(get_settings().mcp_oauth_issuer_url)],
        "scopes_supported": [
            MCP_ACCESS_SCOPE,
            TASKS_CREATE_SCOPE,
            SUBMISSIONS_READ_SCOPE,
            SUBMISSIONS_VERIFY_SCOPE,
            SUBMISSIONS_APPROVE_SCOPE,
            PAYMENTS_READ_SCOPE,
            PAYMENTS_WRITE_SCOPE,
        ],
        "bearer_methods_supported": ["header"],
    }
    assert metadata_options.status_code == 200
    assert metadata_options.headers["access-control-allow-origin"] == "*"
    assert "GET" in metadata_options.headers["access-control-allow-methods"]
    assert missing.status_code == invalid.status_code == query_token.status_code == 401
    assert legacy_key.status_code == 401
    assert missing.json() == invalid.json()
    challenge = missing.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="http://127.0.0.1:8000/' in challenge
    assert f'scope="{MCP_ACCESS_SCOPE}"' in challenge
    assert TASKS_CREATE_SCOPE not in challenge
    assert SUBMISSIONS_VERIFY_SCOPE not in challenge
    assert SUBMISSIONS_APPROVE_SCOPE not in challenge
    assert initialized.status_code == 200
    listed_tool_names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {
        "create_task",
        "get_submission_proofs",
        "verify_submission",
        "start_task_payment_authorization",
        "refresh_task_payment_authorization",
        "get_payment_status",
        "get_wallet_balance",
    } <= listed_tool_names
    assert called.json()["result"]["structuredContent"]["created_via"] == "mcp"
    assert rebinding.status_code == 421


async def test_create_task_tool_schema_replay_and_safe_audit(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    token, principal = await issue_client(creator_id)
    arguments = mcp_arguments()

    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            listed = await mcp_client.list_tools()
            tool = next(item for item in listed.tools if item.name == "create_task")
            tool_schema = tool.model_dump(mode="json", by_alias=True)["inputSchema"]
            proof_schema = tool_schema["$defs"]["BountyCreate"]["properties"]["proof_requirements"]
            assert proof_schema["uniqueItems"] is True
            assert proof_schema["items"]["enum"] == ["url", "screenshot", "image"]
            assert tool.annotations is not None
            assert tool.annotations.idempotent_hint is True

            created = await mcp_client.call_tool("create_task", arguments)
            normalized_retry = copy.deepcopy(arguments)
            normalized_retry["title"] = "  Launch campaign  "
            normalized_retry["currency"] = " usd "
            normalized_retry["bounties"][0]["title"] = "  Comment on a launch thread  "
            replayed = await mcp_client.call_tool("create_task", normalized_retry)

    assert not created.is_error
    assert not replayed.is_error
    assert created.structured_content == replayed.structured_content
    assert created.structured_content["created_via"] == "mcp"

    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        assert await session.scalar(select(func.count()).select_from(Bounty)) == 1
        request = await session.scalar(select(MCPRequest))
        assert request is not None
        assert request.status == RequestStatus.SUCCEEDED
        assert request.api_client_id is None
        assert request.oauth_delegation_id == principal.delegation_id
        assert request.actor_user_id == creator_id
        assert request.auth_scopes == [MCP_ACCESS_SCOPE, TASKS_CREATE_SCOPE]
        assert request.oauth_consent_version == principal.consent_version
        assert request.oauth_authorization_id == principal.authorization_id
        assert str(request.task_id) == created.structured_content["id"]
        assert request.request_data["bounty_count"] == 1
        assert "description" not in request.request_data
        assert "bounties" not in request.request_data
        assert request.response_data["task_id"] == created.structured_content["id"]
        assert "description" not in request.response_data
        serialized_audit = json.dumps(
            {"request": request.request_data, "response": request.response_data}
        )
        assert token not in serialized_audit


async def test_create_task_scope_denied_before_audit(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id, scopes=set())
    result = await call_create_tool(principal, mcp_arguments())

    assert result.is_error
    assert TASKS_CREATE_SCOPE in result.content[0].text
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 0


async def test_mcp_time_validation_is_structured_and_audited(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    token, principal = await issue_client(creator_id)
    result = await call_create_tool(
        principal,
        mcp_arguments(deadline_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()),
    )

    assert result.is_error
    error_text = result.content[0].text
    assert '"error":"validation_error"' in error_text
    assert '"request_id"' in error_text
    assert "deadline_at" in error_text
    assert token not in error_text
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        assert request is not None
        assert request.status == RequestStatus.FAILED
        assert request.error_message == "task input is invalid"
        assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_http_and_mcp_reject_values_outside_database_ranges(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id)
    manual = await client.post(
        "/v1/tasks",
        json=task_payload(creator_id, total_budget_minor=POSTGRES_BIGINT_MAX + 1),
    )
    through_mcp = await call_create_tool(
        principal,
        mcp_arguments(total_budget_minor=POSTGRES_BIGINT_MAX + 1),
    )

    assert manual.status_code == 422
    assert through_mcp.is_error
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 0


async def test_idempotency_conflict_preserves_first_result(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id)
    arguments = mcp_arguments()
    created = await call_create_tool(principal, arguments)
    conflicting_arguments = copy.deepcopy(arguments)
    conflicting_arguments["description"] = "A different task"
    conflict = await call_create_tool(principal, conflicting_arguments)

    assert not created.is_error
    assert conflict.is_error
    assert "different input" in conflict.content[0].text
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        request = await session.scalar(select(MCPRequest))
        assert request is not None
        assert request.status == RequestStatus.SUCCEEDED
        assert request.response_data["task_id"] == created.structured_content["id"]
        assert request.response_data["status"] == "created"


async def test_concurrent_duplicate_mcp_calls_create_one_task(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id)
    arguments = mcp_arguments(idempotency_key="concurrent-task")
    data = task_command(arguments)

    first, second = await asyncio.gather(
        create_task_from_mcp(
            principal,
            idempotency_key=str(arguments["idempotency_key"]),
            data=data,
        ),
        create_task_from_mcp(
            principal,
            idempotency_key=str(arguments["idempotency_key"]),
            data=data,
        ),
    )

    assert first == second
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 1


async def test_failed_request_is_redacted_and_retryable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = await create_user(client)
    token, principal = await issue_client(
        creator_id,
        scopes={TASKS_CREATE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )
    arguments = mcp_arguments(idempotency_key="retry-task")
    data = task_command(arguments)
    real_create_task = mcp_requests.create_task
    call_count = 0

    async def fail_once(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError(f"downstream leaked {token}")
        return await real_create_task(*args, **kwargs)

    monkeypatch.setattr(mcp_requests, "create_task", fail_once)
    with pytest.raises(MCPRequestExecutionError) as captured:
        await create_task_from_mcp(
            principal,
            idempotency_key=str(arguments["idempotency_key"]),
            data=data,
        )
    assert token not in str(captured.value)

    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        assert request is not None
        assert request.status == RequestStatus.FAILED
        assert request.error_message == "task creation failed"
        assert token not in json.dumps(request.request_data)
        assert await session.scalar(select(func.count()).select_from(Task)) == 0

    refreshed_principal = replace(
        principal,
        scopes=frozenset({MCP_ACCESS_SCOPE, TASKS_CREATE_SCOPE}),
    )
    retried = await create_task_from_mcp(
        refreshed_principal,
        idempotency_key=str(arguments["idempotency_key"]),
        data=data,
    )
    assert retried.created_via == "mcp"
    assert call_count == 2


async def test_failed_request_cannot_retry_under_a_new_oauth_authorization(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id)
    arguments = mcp_arguments(idempotency_key="authorization-snapshot-retry")
    data = task_command(arguments)
    call_count = 0

    async def fail_creation(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("downstream unavailable")

    monkeypatch.setattr(mcp_requests, "create_task", fail_creation)
    with pytest.raises(MCPRequestExecutionError):
        await create_task_from_mcp(
            principal,
            idempotency_key=str(arguments["idempotency_key"]),
            data=data,
        )

    new_authorization_id = f"test-authorization-{uuid4()}"
    async with AsyncSessionFactory() as session:
        delegation = await grant_oauth_delegation(
            session,
            user_id=principal.user_id,
            issuer=principal.issuer,
            subject=principal.subject,
            oauth_client_id=principal.client_id,
            authorization_id=new_authorization_id,
            scopes=principal.scopes,
        )
    reauthorized = replace(
        principal,
        authorization_id=new_authorization_id,
        consent_version=delegation.consent_version,
    )

    with pytest.raises(IdempotencyConflictError, match="authorization snapshot"):
        await create_task_from_mcp(
            reauthorized,
            idempotency_key=str(arguments["idempotency_key"]),
            data=data,
        )

    assert call_count == 1
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        assert request is not None
        assert request.status == RequestStatus.FAILED
        assert request.oauth_consent_version == principal.consent_version
        assert request.oauth_authorization_id == principal.authorization_id
        assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_reservation_failure_returns_only_stable_safe_error(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = await create_user(client)
    token, principal = await issue_client(creator_id)
    arguments = mcp_arguments(idempotency_key="reservation-failure")

    async def fail_reservation(**kwargs):
        raise RuntimeError(f"raw database details and secret {token}")

    monkeypatch.setattr(mcp_requests, "_reserve_request", fail_reservation)
    with pytest.raises(MCPRequestExecutionError) as captured:
        await create_task_from_mcp(
            principal,
            idempotency_key=str(arguments["idempotency_key"]),
            data=task_command(arguments),
        )

    error_text = str(captured.value)
    assert "task creation failed" in error_text
    assert str(captured.value.request_id) in error_text
    assert token not in error_text
    assert "raw database details" not in error_text
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 0
        assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_successful_replay_does_not_execute_and_cannot_be_marked_failed(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id)
    arguments = mcp_arguments(idempotency_key="stable-success")
    data = task_command(arguments)
    created = await create_task_from_mcp(
        principal,
        idempotency_key=str(arguments["idempotency_key"]),
        data=data,
    )
    opened = await client.post(
        f"/v1/tasks/{created.id}/open",
        headers=client.auth_headers(creator_id),
    )
    assert opened.status_code == 200
    assert opened.json()["status"] == "open"

    async def must_not_execute(*args, **kwargs):
        raise AssertionError("successful request executed twice")

    def must_not_validate(*args, **kwargs):
        raise AssertionError("successful request was revalidated")

    monkeypatch.setattr(mcp_requests, "create_task", must_not_execute)
    monkeypatch.setattr(mcp_requests, "TaskCreate", must_not_validate)
    replayed = await create_task_from_mcp(
        principal,
        idempotency_key=str(arguments["idempotency_key"]),
        data=data,
    )
    assert replayed == created

    async with AsyncSessionFactory() as session:
        request_id = await session.scalar(select(MCPRequest.id))
    assert request_id is not None
    await _mark_failed(request_id, "late failure")
    async with AsyncSessionFactory() as session:
        request = await session.get(MCPRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.SUCCEEDED
        assert request.error_message is None


def comparable_task(data: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(data)
    for key in ("id", "created_at", "updated_at", "created_via"):
        result.pop(key)
    for bounty in result["bounties"]:
        for key in ("id", "created_at", "updated_at"):
            bounty.pop(key)
    return result


async def test_manual_and_mcp_task_creation_have_business_parity(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    _, principal = await issue_client(creator_id)
    manual_payload = task_payload(creator_id)
    manual = await client.post("/v1/tasks", json=manual_payload)
    assert manual.status_code == 201

    arguments = {key: value for key, value in manual_payload.items() if key != "creator_id"}
    arguments["idempotency_key"] = "parity-task"
    through_mcp = await call_create_tool(principal, arguments)

    assert not through_mcp.is_error
    assert manual.json()["created_via"] == "manual"
    assert through_mcp.structured_content["created_via"] == "mcp"
    assert comparable_task(manual.json()) == comparable_task(through_mcp.structured_content)
