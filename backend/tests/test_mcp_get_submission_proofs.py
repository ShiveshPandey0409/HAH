from __future__ import annotations

import base64
from uuid import UUID

from mcp import Client
from sqlalchemy import func, select

from app.db.session import AsyncSessionFactory
from app.main import app
from app.mcp.oauth import OAuthPrincipal, use_oauth_principal
from app.models.integration import MCPRequest
from app.models.submission import ProofUpload
from app.services.api_clients import SUBMISSIONS_READ_SCOPE, TASKS_CREATE_SCOPE
from tests.test_marketplace import create_user
from tests.test_mcp_create_task import issue_client
from tests.test_submissions import (
    PNG_PROOF,
    claimed_work,
    screenshot_proof,
    submit,
    upload_image_proof,
    url_proof,
)


async def call_get_proofs(principal: OAuthPrincipal, submission_id: UUID):
    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            return await mcp_client.call_tool(
                "get_submission_proofs",
                {"submission_id": str(submission_id)},
            )


async def test_mcp_reads_owned_url_and_image_proofs_with_read_scope(client) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(
        client,
        "mcp-proof-read",
        proof_requirements=["url", "screenshot"],
    )
    uploaded = await upload_image_proof(client, claim_id, freelancer_id)
    assert uploaded.status_code == 201, uploaded.text
    created = await submit(
        client,
        claim_id,
        freelancer_id,
        [url_proof(), screenshot_proof(uploaded.json()["upload_id"])],
    )
    assert created.status_code == 201, created.text
    submission_id = UUID(created.json()["id"])
    _, principal = await issue_client(creator_id, scopes={SUBMISSIONS_READ_SCOPE})

    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            tools = await mcp_client.list_tools()
            tool = next(item for item in tools.tools if item.name == "get_submission_proofs")
            result = await mcp_client.call_tool(
                "get_submission_proofs",
                {"submission_id": str(submission_id)},
            )

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert not result.is_error
    assert result.structured_content["id"] == str(submission_id)
    assert [proof["proof_type"] for proof in result.structured_content["proofs"]] == [
        "url",
        "screenshot",
    ]
    assert len(result.content) == 2
    assert result.content[1].type == "image"
    assert result.content[1].mime_type == "image/png"
    assert base64.b64decode(result.content[1].data) == PNG_PROOF

    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(ProofUpload)) == 1
        assert await session.scalar(select(func.count()).select_from(MCPRequest)) == 0


async def test_mcp_proof_reader_requires_scope_and_hides_other_creators(client) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "mcp-proof-hidden")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    submission_id = UUID(created.json()["id"])

    _, missing_scope = await issue_client(creator_id, scopes={TASKS_CREATE_SCOPE})
    denied = await call_get_proofs(missing_scope, submission_id)
    assert denied.is_error
    assert SUBMISSIONS_READ_SCOPE in denied.content[0].text

    other_creator_id = await create_user(
        client,
        email="other-proof-reader@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    _, other_creator = await issue_client(
        other_creator_id,
        scopes={SUBMISSIONS_READ_SCOPE},
    )
    hidden = await call_get_proofs(other_creator, submission_id)
    assert hidden.is_error
    assert "Submission not found" in hidden.content[0].text
