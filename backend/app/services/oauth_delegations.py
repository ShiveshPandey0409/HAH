from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.mcp.oauth import MCP_ACCESS_SCOPE
from app.models.integration import (
    IntegrationStatus,
    OAuthAuthorizationGrant,
    OAuthDelegation,
    OAuthIdentity,
)
from app.models.user import User
from app.services.api_clients import SUPPORTED_SCOPES

MAX_OAUTH_IDENTIFIER_LENGTH = 2_048
OAUTH_SUPPORTED_SCOPES = frozenset({MCP_ACCESS_SCOPE, *SUPPORTED_SCOPES})


class OAuthDelegationValidationError(Exception):
    pass


class OAuthIdentityConflictError(Exception):
    pass


def _exact_identifier(value: str, *, name: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > MAX_OAUTH_IDENTIFIER_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise OAuthDelegationValidationError(f"{name} is invalid")
    return value


def _approved_scopes(scopes: set[str] | frozenset[str]) -> list[str]:
    unsupported = set(scopes) - OAUTH_SUPPORTED_SCOPES
    if unsupported:
        raise OAuthDelegationValidationError("OAuth delegation contains an unsupported scope")
    if MCP_ACCESS_SCOPE not in scopes:
        raise OAuthDelegationValidationError(f"OAuth delegation requires {MCP_ACCESS_SCOPE}")
    return sorted(scopes)


async def grant_oauth_delegation(
    session: AsyncSession,
    *,
    user_id: UUID,
    issuer: str,
    subject: str,
    oauth_client_id: str,
    authorization_id: str,
    scopes: set[str] | frozenset[str],
    commit: bool = True,
) -> OAuthDelegation:
    """Record trusted account linking and explicit consent from the external AS flow.

    ``authorization_id`` is an authorization-server-attested grant handle. It
    must remain stable for access and refresh tokens from one consent and rotate
    whenever the authorization server creates a new consent grant.
    """

    issuer = _exact_identifier(issuer, name="issuer")
    subject = _exact_identifier(subject, name="subject")
    oauth_client_id = _exact_identifier(oauth_client_id, name="OAuth client ID")
    authorization_id = _exact_identifier(authorization_id, name="authorization ID")
    approved_scopes = _approved_scopes(scopes)
    if issuer != str(get_settings().mcp_oauth_issuer_url):
        raise OAuthDelegationValidationError("issuer is not the configured authorization server")

    user = await session.get(User, user_id)
    if user is None or not user.can_create_tasks:
        raise OAuthDelegationValidationError("OAuth MCP delegation requires a task creator")

    try:
        await session.execute(
            insert(OAuthIdentity)
            .values(
                user_id=user_id,
                issuer=issuer,
                subject=subject,
                status=IntegrationStatus.ACTIVE,
            )
            .on_conflict_do_nothing(index_elements=[OAuthIdentity.issuer, OAuthIdentity.subject])
        )
        identity = await session.scalar(
            select(OAuthIdentity)
            .where(
                OAuthIdentity.issuer == issuer,
                OAuthIdentity.subject == subject,
            )
            .with_for_update()
        )
        if identity is None:
            raise RuntimeError("OAuth identity could not be loaded")
        if identity.user_id != user_id:
            raise OAuthIdentityConflictError(
                "OAuth issuer and subject are already linked to another user"
            )
        if identity.status != IntegrationStatus.ACTIVE:
            raise OAuthDelegationValidationError("OAuth identity is disabled")

        delegation = await session.scalar(
            select(OAuthDelegation)
            .where(
                OAuthDelegation.identity_id == identity.id,
                OAuthDelegation.oauth_client_id == oauth_client_id,
            )
            .with_for_update()
        )
        if delegation is None:
            consented_at = datetime.now(UTC)
            delegation = OAuthDelegation(
                identity_id=identity.id,
                oauth_client_id=oauth_client_id,
                approved_scopes=approved_scopes,
                status=IntegrationStatus.ACTIVE,
                consent_version=1,
                consented_at=consented_at,
                authorization_id=authorization_id,
            )
            session.add(delegation)
        else:
            reconsent_required = (
                delegation.status != IntegrationStatus.ACTIVE
                or delegation.revoked_at is not None
                or delegation.approved_scopes != approved_scopes
            )
            authorization_changed = delegation.authorization_id != authorization_id
            if reconsent_required and not authorization_changed:
                raise OAuthDelegationValidationError(
                    "fresh consent requires a new authorization ID"
                )
            if authorization_changed:
                reused_authorization = await session.scalar(
                    select(OAuthAuthorizationGrant.delegation_id).where(
                        OAuthAuthorizationGrant.delegation_id == delegation.id,
                        OAuthAuthorizationGrant.authorization_id == authorization_id,
                    )
                )
                if reused_authorization is not None:
                    raise OAuthDelegationValidationError(
                        "authorization ID was already used for this delegation"
                    )
                consented_at = datetime.now(UTC)
                if consented_at <= delegation.consented_at:
                    consented_at = delegation.consented_at + timedelta(microseconds=1)
                delegation.approved_scopes = approved_scopes
                delegation.status = IntegrationStatus.ACTIVE
                delegation.revoked_at = None
                delegation.consent_version += 1
                delegation.consented_at = consented_at
                delegation.authorization_id = authorization_id

        if commit:
            await session.commit()
            await session.refresh(delegation)
        else:
            await session.flush()
        return delegation
    except Exception:
        await session.rollback()
        raise


async def revoke_oauth_delegation(
    session: AsyncSession,
    *,
    delegation_id: UUID,
) -> OAuthDelegation:
    delegation = await session.scalar(
        select(OAuthDelegation).where(OAuthDelegation.id == delegation_id).with_for_update()
    )
    if delegation is None:
        raise OAuthDelegationValidationError("OAuth delegation does not exist")
    if delegation.status == IntegrationStatus.DISABLED and delegation.revoked_at is not None:
        await session.commit()
        await session.refresh(delegation)
        return delegation

    revoked_at = datetime.now(UTC)
    if revoked_at < delegation.consented_at:
        revoked_at = delegation.consented_at
    delegation.status = IntegrationStatus.DISABLED
    delegation.revoked_at = revoked_at
    delegation.consent_version += 1
    try:
        await session.commit()
        await session.refresh(delegation)
        return delegation
    except Exception:
        await session.rollback()
        raise
