from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from mcp import Client
from sqlalchemy import func, select

from app.core.redaction import canonical_json_fingerprint, redact_sensitive_data
from app.db.session import AsyncSessionFactory
from app.mcp.oauth import MCP_ACCESS_SCOPE, OAuthPrincipal, use_oauth_principal
from app.models.claim import BountyClaim, ClaimStatus
from app.models.integration import IntegrationStatus, MCPRequest, RequestStatus
from app.models.submission import Submission, VerificationMethod, VerificationStatus
from app.models.webhook import WebhookDelivery, WebhookEndpoint
from app.services import mcp_requests
from app.services.api_clients import (
    SUBMISSIONS_APPROVE_SCOPE,
    SUBMISSIONS_VERIFY_SCOPE,
    TASKS_CREATE_SCOPE,
)
from app.services.mcp_requests import (
    MCPRequestExecutionError,
    MCPRequestValidationError,
    create_task_from_mcp,
    verify_submission_from_mcp,
)
from app.services.webhooks import ENCRYPTED_WEBHOOK_URL
from tests.test_marketplace import create_user
from tests.test_mcp_create_task import issue_client, mcp_arguments, task_command
from tests.test_submissions import claimed_work, submit, url_proof


def verification_arguments(
    submission_id: UUID,
    **overrides: object,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "idempotency_key": "submission-verify-001",
        "submission_id": str(submission_id),
        "result": "passed",
        "checks": {"reachable": True, "platform_matches": True},
    }
    arguments.update(overrides)
    return arguments


async def call_verify_tool(
    principal: OAuthPrincipal,
    arguments: dict[str, object],
):
    from app.main import app

    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            return await mcp_client.call_tool("verify_submission", arguments)


async def submitted_work(client, label: str) -> tuple[UUID, UUID]:
    creator_id, freelancer_id, claim_id = await claimed_work(client, label)
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text
    return creator_id, UUID(created.json()["id"])


async def enable_webhook(creator_id: UUID, *event_types: str) -> None:
    async with AsyncSessionFactory() as session:
        session.add(
            WebhookEndpoint(
                creator_id=creator_id,
                url=ENCRYPTED_WEBHOOK_URL,
                secret_hash=f"sha256:{'0' * 64}",
                secret_ciphertext=b"encrypted-for-test",
                subscribed_events=list(event_types),
                status=IntegrationStatus.ACTIVE,
            )
        )
        await session.commit()


async def test_verify_tool_schema_replay_safe_audit_and_transactional_events(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-success")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_APPROVE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )
    await enable_webhook(
        creator_id,
        "verification.completed",
        "mcp_request.completed",
    )
    raw_secret = "provider-secret-must-not-persist"
    embedded_secret = "embedded-bearer-must-not-persist"
    key_secret = "credential-key-must-not-persist"
    assigned_key_secret = "assigned-key-secret-must-not-persist"
    identifier_key_secret = "sk_live_identifier_secret_must_not_persist"
    assignment_value_secret = "assignment_value_secret_must_not_persist"
    checks = {
        "reachable": True,
        "provider_secret": raw_secret,
        "notes": f"provider replied with Bearer {embedded_secret}",
        f"Authorization: Bearer {key_secret}": "credential-bearing key",
        f"api_key={assigned_key_secret}": "assigned credential-bearing key",
        f"api_key_{identifier_key_secret}": "identifier credential-bearing key",
        "assignment_notes": f"provider returned token={assignment_value_secret}",
    }
    arguments = verification_arguments(submission_id, checks=checks)

    from app.main import app

    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            tools = await mcp_client.list_tools()
            tool = next(item for item in tools.tools if item.name == "verify_submission")
            schema = tool.model_dump(mode="json", by_alias=True)["inputSchema"]
            assert schema["properties"]["result"]["enum"] == [
                "passed",
                "failed",
                "review_required",
            ]
            assert tool.annotations is not None
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.destructive_hint is True

            verified = await mcp_client.call_tool("verify_submission", arguments)
            replay_arguments = dict(arguments)
            replay_arguments["idempotency_key"] = "  submission-verify-001  "
            replayed = await mcp_client.call_tool(
                "verify_submission",
                replay_arguments,
            )

    assert not verified.is_error
    assert not replayed.is_error
    assert replayed.structured_content == verified.structured_content
    assert verified.structured_content["verification_method"] == "mcp"
    assert verified.structured_content["verification_status"] == "passed"
    assert verified.structured_content["claim_status"] == "approved"
    assert verified.structured_content["checks"]["provider_secret"] == "[REDACTED]"
    assert verified.structured_content["checks"]["notes"] == "[REDACTED]"
    assert verified.structured_content["checks"]["assignment_notes"] == "[REDACTED]"
    assert verified.structured_content["checks"]["[REDACTED_KEY]"] == "[REDACTED]"
    assert verified.structured_content["checks"]["[REDACTED_KEY_2]"] == "[REDACTED]"
    assert verified.structured_content["checks"]["[REDACTED_KEY_3]"] == "[REDACTED]"
    assert verified.structured_content["proofs"][0]["url"] is None
    assert verified.structured_content["proofs"][0]["storage_key"] is None

    expected_fingerprint = canonical_json_fingerprint(
        redact_sensitive_data(
            {
                "submission_id": str(submission_id),
                "result": "passed",
                "checks": checks,
                "failure_reason": None,
            }
        )
    )
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
        deliveries = list(
            (
                await session.scalars(select(WebhookDelivery).order_by(WebhookDelivery.event_type))
            ).all()
        )

    assert request is not None
    assert request.method == "verify_submission"
    assert request.status == RequestStatus.SUCCEEDED
    assert request.api_client_id is None
    assert request.oauth_delegation_id == principal.delegation_id
    assert request.actor_user_id == creator_id
    assert request.auth_scopes == [
        MCP_ACCESS_SCOPE,
        SUBMISSIONS_APPROVE_SCOPE,
        SUBMISSIONS_VERIFY_SCOPE,
    ]
    assert request.oauth_consent_version == principal.consent_version
    assert request.oauth_authorization_id == principal.authorization_id
    assert request.submission_id == submission_id
    assert request.request_data["input_fingerprint"] == expected_fingerprint
    assert request.request_data["check_count"] == 7
    assert raw_secret not in json.dumps(request.request_data)
    assert raw_secret not in json.dumps(request.response_data)
    assert embedded_secret not in json.dumps(request.request_data)
    assert embedded_secret not in json.dumps(request.response_data)
    assert key_secret not in json.dumps(request.request_data)
    assert key_secret not in json.dumps(request.response_data)
    assert assigned_key_secret not in json.dumps(request.request_data)
    assert assigned_key_secret not in json.dumps(request.response_data)
    assert identifier_key_secret not in json.dumps(request.request_data)
    assert identifier_key_secret not in json.dumps(request.response_data)
    assert assignment_value_secret not in json.dumps(request.request_data)
    assert assignment_value_secret not in json.dumps(request.response_data)
    assert request.response_data == verified.structured_content
    assert submission is not None
    assert submission.verification_method == VerificationMethod.MCP
    assert submission.verification_status == VerificationStatus.PASSED
    assert [delivery.event_type for delivery in deliveries] == [
        "mcp_request.completed",
        "verification.completed",
    ]
    rendered_events = b"\n".join(delivery.payload_body for delivery in deliveries).decode()
    assert raw_secret not in rendered_events
    assert embedded_secret not in rendered_events
    assert key_secret not in rendered_events
    assert assigned_key_secret not in rendered_events
    assert identifier_key_secret not in rendered_events
    assert assignment_value_secret not in rendered_events
    assert "idempotency_key" not in rendered_events
    assert "request_data" not in rendered_events


async def test_failure_reason_credentials_are_redacted_before_persistence(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-failure-reason")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_VERIFY_SCOPE},
    )
    await enable_webhook(
        creator_id,
        "verification.completed",
        "mcp_request.completed",
    )
    reason_secret = "failure-reason-secret-must-not-persist"

    verified = await call_verify_tool(
        principal,
        verification_arguments(
            submission_id,
            result="failed",
            checks={"reachable": False},
            failure_reason=f"provider failed with api_key={reason_secret}",
        ),
    )

    assert not verified.is_error
    assert verified.structured_content["verification_status"] == "failed"
    assert verified.structured_content["claim_status"] == "rejected"
    assert verified.structured_content["failure_reason"] == "[REDACTED]"

    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
        deliveries = list((await session.scalars(select(WebhookDelivery))).all())

    assert request is not None
    assert submission is not None
    assert submission.verification_note == "[REDACTED]"
    assert reason_secret not in json.dumps(request.request_data)
    assert reason_secret not in json.dumps(request.response_data)
    rendered_events = b"\n".join(delivery.payload_body for delivery in deliveries).decode()
    assert reason_secret not in rendered_events


async def test_verify_scope_is_required_before_audit(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-scope")
    _, principal = await issue_client(creator_id, scopes={TASKS_CREATE_SCOPE})

    result = await call_verify_tool(principal, verification_arguments(submission_id))

    assert result.is_error
    assert SUBMISSIONS_VERIFY_SCOPE in result.content[0].text
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 0
        submission = await session.get(Submission, submission_id)
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PENDING


async def test_approve_scope_is_required_before_passed_audit(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-approve-scope")
    _, principal = await issue_client(creator_id, scopes={SUBMISSIONS_VERIFY_SCOPE})

    result = await call_verify_tool(principal, verification_arguments(submission_id))

    assert result.is_error
    assert SUBMISSIONS_APPROVE_SCOPE in result.content[0].text
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 0
        submission = await session.get(Submission, submission_id)
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PENDING


async def test_verify_idempotency_conflict_preserves_first_result(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-conflict")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_APPROVE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )
    arguments = verification_arguments(submission_id)
    first = await call_verify_tool(principal, arguments)
    conflicting = dict(arguments)
    conflicting["checks"] = {"reachable": False}
    conflict = await call_verify_tool(principal, conflicting)

    assert not first.is_error
    assert conflict.is_error
    assert "different input" in conflict.content[0].text
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 1
    assert request is not None
    assert request.status == RequestStatus.SUCCEEDED
    assert request.response_data == first.structured_content
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PASSED


async def test_invalid_verification_is_safely_audited(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-invalid")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_VERIFY_SCOPE},
    )
    raw_secret = "validation-secret"
    result = await call_verify_tool(
        principal,
        verification_arguments(
            submission_id,
            result="failed",
            checks={"access_token": raw_secret},
        ),
    )

    assert result.is_error
    assert '"error":"validation_error"' in result.content[0].text
    assert "failure_reason" in result.content[0].text
    assert raw_secret not in result.content[0].text
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
    assert request is not None
    assert request.status == RequestStatus.FAILED
    assert request.submission_id == submission_id
    assert request.error_message == "submission verification input is invalid"
    assert request.request_data["check_count"] == 1
    assert raw_secret not in json.dumps(request.request_data)
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PENDING


async def test_oversized_checks_do_not_bloat_the_failed_audit(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-oversized-checks")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_APPROVE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )

    with pytest.raises(MCPRequestValidationError):
        await verify_submission_from_mcp(
            principal,
            idempotency_key="oversized-checks",
            submission_id=submission_id,
            result="passed",
            checks={"details": "x" * 20_000},
        )

    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
    assert request is not None
    assert request.status == RequestStatus.FAILED
    assert request.request_data["check_count"] == 1
    assert len(json.dumps(request.request_data)) < 1_000


async def test_wrong_creator_is_hidden_and_cannot_verify(client) -> None:
    _, submission_id = await submitted_work(client, "mcp-wrong-creator")
    wrong_creator_id = await create_user(
        client,
        email="mcp-wrong-creator-agent@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    _, principal = await issue_client(
        wrong_creator_id,
        scopes={SUBMISSIONS_APPROVE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )
    await enable_webhook(wrong_creator_id, "mcp_request.completed")

    result = await call_verify_tool(principal, verification_arguments(submission_id))

    assert result.is_error
    assert "submission not found" in result.content[0].text
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
        delivery = await session.scalar(select(WebhookDelivery))
    assert request is not None
    assert request.status == RequestStatus.FAILED
    assert request.submission_id is None
    assert request.error_message == "submission not found"
    assert delivery is not None
    assert delivery.payload["data"]["submission_id"] is None
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PENDING


async def test_duplicate_calls_lock_one_request_and_verify_once(client) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-concurrent")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_APPROVE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )
    arguments = {
        "idempotency_key": "concurrent-verification",
        "submission_id": submission_id,
        "result": "passed",
        "checks": {"reachable": True},
    }

    first, second = await asyncio.gather(
        verify_submission_from_mcp(principal, **arguments),
        verify_submission_from_mcp(principal, **arguments),
    )

    assert first == second
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 1
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
    assert request is not None
    assert request.status == RequestStatus.SUCCEEDED
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PASSED


async def test_completion_event_failure_rolls_back_verification_then_retry_succeeds(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id, submission_id = await submitted_work(client, "mcp-atomic")
    _, principal = await issue_client(
        creator_id,
        scopes={SUBMISSIONS_APPROVE_SCOPE, SUBMISSIONS_VERIFY_SCOPE},
    )
    real_enqueue = mcp_requests._enqueue_mcp_request_completed
    call_count = 0

    async def fail_once(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("downstream event failure with private details")
        return await real_enqueue(*args, **kwargs)

    monkeypatch.setattr(mcp_requests, "_enqueue_mcp_request_completed", fail_once)
    command = {
        "idempotency_key": "atomic-verification",
        "submission_id": submission_id,
        "result": "passed",
        "checks": {"reachable": True},
    }
    with pytest.raises(MCPRequestExecutionError) as captured:
        await verify_submission_from_mcp(principal, **command)
    assert "private details" not in str(captured.value)

    async with AsyncSessionFactory() as session:
        failed_request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
        assert submission is not None
        claim = await session.get(BountyClaim, submission.claim_id)
    assert failed_request is not None
    assert failed_request.status == RequestStatus.FAILED
    assert submission.verification_status == VerificationStatus.PENDING
    assert claim is not None
    assert claim.status == ClaimStatus.SUBMITTED

    retried = await verify_submission_from_mcp(principal, **command)
    assert retried.verification_status == VerificationStatus.PASSED
    async with AsyncSessionFactory() as session:
        request = await session.scalar(select(MCPRequest))
        submission = await session.get(Submission, submission_id)
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 1
    assert request is not None
    assert request.status == RequestStatus.SUCCEEDED
    assert request.error_message is None
    assert submission is not None
    assert submission.verification_status == VerificationStatus.PASSED


async def test_create_task_emits_mcp_completion_event(client) -> None:
    creator_id = await create_user(
        client,
        email="mcp-task-completion-event@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    _, principal = await issue_client(creator_id, scopes={TASKS_CREATE_SCOPE})
    await enable_webhook(creator_id, "mcp_request.completed")
    arguments = mcp_arguments(idempotency_key=f"task-event-{uuid4()}")

    created = await create_task_from_mcp(
        principal,
        idempotency_key=str(arguments["idempotency_key"]),
        data=task_command(arguments),
    )

    async with AsyncSessionFactory() as session:
        delivery = await session.scalar(select(WebhookDelivery))
    assert delivery is not None
    assert delivery.event_type == "mcp_request.completed"
    assert delivery.entity_type == "mcp_request"
    assert delivery.payload["data"]["method"] == "create_task"
    assert delivery.payload["data"]["status"] == "succeeded"
    assert delivery.payload["data"]["task_id"] == str(created.id)
    assert "idempotency_key" not in delivery.payload_body.decode()
