from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.mcp.oauth import MCP_ACCESS_SCOPE
from app.models.integration import IntegrationStatus, OAuthIdentity
from app.models.user import User
from app.services.api_clients import SUBMISSIONS_APPROVE_SCOPE, TASKS_CREATE_SCOPE
from app.services.oauth_delegations import (
    OAuthDelegationValidationError,
    OAuthIdentityConflictError,
    grant_oauth_delegation,
    revoke_oauth_delegation,
)

ISSUER = str(get_settings().mcp_oauth_issuer_url)
SUBJECT = "stable-external-subject"
CLIENT_ID = "external-agent-client"
AUTHORIZATION_ID_V1 = "authorization-grant-v1"


async def create_creator(email: str, *, can_create_tasks: bool = True) -> UUID:
    async with AsyncSessionFactory() as session:
        user = User(
            email=email,
            display_name="OAuth Delegation User",
            can_create_tasks=can_create_tasks,
            can_work_tasks=not can_create_tasks,
        )
        session.add(user)
        await session.commit()
        return user.id


async def test_password_login_and_mcp_oauth_share_one_canonical_user(
    client: AsyncClient,
) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "email": "shared-http-mcp@example.com",
            "password": "correct horse battery staple",
            "display_name": "Shared HTTP and MCP User",
            "can_create_tasks": True,
            "can_work_tasks": False,
        },
    )
    assert signup.status_code == 201
    user_id = UUID(signup.json()["user"]["id"])

    async with AsyncSessionFactory() as session:
        delegation = await grant_oauth_delegation(
            session,
            user_id=user_id,
            issuer=ISSUER,
            subject="shared-http-mcp-subject",
            oauth_client_id=CLIENT_ID,
            authorization_id="shared-http-mcp-authorization",
            scopes={MCP_ACCESS_SCOPE, TASKS_CREATE_SCOPE},
        )

    async with AsyncSessionFactory() as session:
        identity_user_id = await session.scalar(
            select(OAuthIdentity.user_id).where(OAuthIdentity.id == delegation.identity_id)
        )
    assert identity_user_id == user_id


async def test_grant_reconsent_revoke_and_exact_identity_binding() -> None:
    creator_id = await create_creator("oauth-grant@example.com")
    other_creator_id = await create_creator("oauth-grant-other@example.com")
    initial_scopes = {MCP_ACCESS_SCOPE, TASKS_CREATE_SCOPE}

    async with AsyncSessionFactory() as session:
        granted = await grant_oauth_delegation(
            session,
            user_id=creator_id,
            issuer=ISSUER,
            subject=SUBJECT,
            oauth_client_id=CLIENT_ID,
            authorization_id=AUTHORIZATION_ID_V1,
            scopes=initial_scopes,
        )
        delegation_id = granted.id
        identity_id = granted.identity_id
        initial_consent_at = granted.consented_at
        assert granted.authorization_id == AUTHORIZATION_ID_V1

    async with AsyncSessionFactory() as session:
        repeated = await grant_oauth_delegation(
            session,
            user_id=creator_id,
            issuer=ISSUER,
            subject=SUBJECT,
            oauth_client_id=CLIENT_ID,
            authorization_id=AUTHORIZATION_ID_V1,
            scopes=initial_scopes,
        )
    assert repeated.id == delegation_id
    assert repeated.identity_id == identity_id
    assert repeated.consent_version == 1
    assert repeated.consented_at == initial_consent_at
    assert repeated.authorization_id == AUTHORIZATION_ID_V1

    async with AsyncSessionFactory() as session:
        with pytest.raises(OAuthDelegationValidationError, match="new authorization ID"):
            await grant_oauth_delegation(
                session,
                user_id=creator_id,
                issuer=ISSUER,
                subject=SUBJECT,
                oauth_client_id=CLIENT_ID,
                authorization_id=AUTHORIZATION_ID_V1,
                scopes={MCP_ACCESS_SCOPE, TASKS_CREATE_SCOPE, SUBMISSIONS_APPROVE_SCOPE},
            )

    async with AsyncSessionFactory() as session:
        expanded = await grant_oauth_delegation(
            session,
            user_id=creator_id,
            issuer=ISSUER,
            subject=SUBJECT,
            oauth_client_id=CLIENT_ID,
            authorization_id="authorization-grant-v2",
            scopes={MCP_ACCESS_SCOPE, TASKS_CREATE_SCOPE, SUBMISSIONS_APPROVE_SCOPE},
        )
    assert expanded.id == delegation_id
    assert expanded.consent_version == 2
    assert expanded.consented_at > initial_consent_at
    assert expanded.authorization_id == "authorization-grant-v2"

    async with AsyncSessionFactory() as session:
        with pytest.raises(OAuthDelegationValidationError, match="already used"):
            await grant_oauth_delegation(
                session,
                user_id=creator_id,
                issuer=ISSUER,
                subject=SUBJECT,
                oauth_client_id=CLIENT_ID,
                authorization_id=AUTHORIZATION_ID_V1,
                scopes=initial_scopes,
            )

    async with AsyncSessionFactory() as session:
        with pytest.raises(OAuthIdentityConflictError):
            await grant_oauth_delegation(
                session,
                user_id=other_creator_id,
                issuer=ISSUER,
                subject=SUBJECT,
                oauth_client_id=CLIENT_ID,
                authorization_id="other-user-authorization",
                scopes=initial_scopes,
            )

    async with AsyncSessionFactory() as session:
        revoked = await revoke_oauth_delegation(session, delegation_id=delegation_id)
    assert revoked.status == IntegrationStatus.DISABLED
    assert revoked.revoked_at is not None
    assert revoked.consent_version == 3
    assert revoked.authorization_id == expanded.authorization_id

    async with AsyncSessionFactory() as session:
        repeated_revoke = await revoke_oauth_delegation(session, delegation_id=delegation_id)
    assert repeated_revoke.consent_version == 3

    async with AsyncSessionFactory() as session:
        reconsented = await grant_oauth_delegation(
            session,
            user_id=creator_id,
            issuer=ISSUER,
            subject=SUBJECT,
            oauth_client_id=CLIENT_ID,
            authorization_id="authorization-grant-v3",
            scopes=initial_scopes,
        )
    assert reconsented.status == IntegrationStatus.ACTIVE
    assert reconsented.revoked_at is None
    assert reconsented.consent_version == 4
    assert reconsented.consented_at > expanded.consented_at
    assert reconsented.authorization_id == "authorization-grant-v3"


async def test_grant_rejects_unapproved_scopes_and_non_creator_users() -> None:
    creator_id = await create_creator("oauth-scope@example.com")
    worker_id = await create_creator(
        "oauth-worker-delegation@example.com",
        can_create_tasks=False,
    )

    for scopes in ({TASKS_CREATE_SCOPE}, {MCP_ACCESS_SCOPE, "payments:write"}):
        async with AsyncSessionFactory() as session:
            with pytest.raises(OAuthDelegationValidationError):
                await grant_oauth_delegation(
                    session,
                    user_id=creator_id,
                    issuer=ISSUER,
                    subject=SUBJECT,
                    oauth_client_id=CLIENT_ID,
                    authorization_id="invalid-scope-authorization",
                    scopes=scopes,
                )

    async with AsyncSessionFactory() as session:
        with pytest.raises(OAuthDelegationValidationError, match="task creator"):
            await grant_oauth_delegation(
                session,
                user_id=worker_id,
                issuer=ISSUER,
                subject="worker-subject",
                oauth_client_id=CLIENT_ID,
                authorization_id="worker-authorization",
                scopes={MCP_ACCESS_SCOPE},
            )

    async with AsyncSessionFactory() as session:
        with pytest.raises(OAuthDelegationValidationError, match="subject"):
            await grant_oauth_delegation(
                session,
                user_id=creator_id,
                issuer=ISSUER,
                subject=" padded ",
                oauth_client_id=CLIENT_ID,
                authorization_id="padded-subject-authorization",
                scopes={MCP_ACCESS_SCOPE},
            )
