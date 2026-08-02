from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionFactory
from app.models.integration import APIClient, IntegrationStatus
from app.models.user import User

TASKS_CREATE_SCOPE = "tasks:create"
SUBMISSIONS_READ_SCOPE = "submissions:read"
SUBMISSIONS_VERIFY_SCOPE = "submissions:verify"
SUBMISSIONS_APPROVE_SCOPE = "submissions:approve"
SUPPORTED_SCOPES = frozenset(
    {
        TASKS_CREATE_SCOPE,
        SUBMISSIONS_READ_SCOPE,
        SUBMISSIONS_VERIFY_SCOPE,
        SUBMISSIONS_APPROVE_SCOPE,
    }
)
TOKEN_PREFIX = "hah"
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)


class APIClientValidationError(Exception):
    pass


class InvalidAPIKeyError(Exception):
    pass


class MissingAPIScopeError(Exception):
    pass


class ScopedPrincipal(Protocol):
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class APIClientPrincipal:
    client_id: UUID
    creator_id: UUID
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class IssuedAPIClient:
    client: APIClient
    token: str = field(repr=False)


def _secret_hash(secret: str) -> str:
    return f"sha256:{hashlib.sha256(secret.encode()).hexdigest()}"


def _parse_token(token: str) -> tuple[str, str] | None:
    if len(token) > 512:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    client_key, secret = parts[1:]
    if not client_key or not secret:
        return None
    return client_key, secret


async def issue_api_client(
    session: AsyncSession,
    *,
    creator_id: UUID,
    name: str,
    scopes: set[str] | frozenset[str],
) -> IssuedAPIClient:
    normalized_name = name.strip()
    if not normalized_name:
        raise APIClientValidationError("API client name cannot be empty")
    unsupported = set(scopes) - SUPPORTED_SCOPES
    if unsupported:
        raise APIClientValidationError("API client contains an unsupported scope")

    creator = await session.get(User, creator_id)
    if creator is None or not creator.can_create_tasks:
        raise APIClientValidationError("API clients require a task creator")

    client_key = secrets.token_urlsafe(18)
    secret = secrets.token_urlsafe(32)
    client = APIClient(
        creator_id=creator_id,
        name=normalized_name,
        client_key=client_key,
        secret_hash=_secret_hash(secret),
        scopes=sorted(scopes),
        status=IntegrationStatus.ACTIVE,
    )
    session.add(client)
    try:
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return IssuedAPIClient(client=client, token=f"{TOKEN_PREFIX}.{client_key}.{secret}")


async def authenticate_api_token(token: str) -> APIClientPrincipal:
    parsed = _parse_token(token)
    client_key, secret = parsed if parsed is not None else ("", "")
    presented_hash = _secret_hash(secret)

    async with AsyncSessionFactory() as session:
        client = await session.scalar(select(APIClient).where(APIClient.client_key == client_key))
        expected_hash = client.secret_hash if client is not None else f"sha256:{'0' * 64}"
        valid_secret = hmac.compare_digest(presented_hash, expected_hash)
        if (
            parsed is None
            or client is None
            or client.status != IntegrationStatus.ACTIVE
            or not valid_secret
        ):
            raise InvalidAPIKeyError

        now = datetime.now(UTC)
        stale_before = now - LAST_USED_WRITE_INTERVAL
        if client.last_used_at is None or client.last_used_at <= stale_before:
            await session.execute(
                update(APIClient)
                .where(
                    APIClient.id == client.id,
                    or_(
                        APIClient.last_used_at.is_(None),
                        APIClient.last_used_at <= stale_before,
                    ),
                )
                .values(last_used_at=now)
            )
            await session.commit()
        return APIClientPrincipal(
            client_id=client.id,
            creator_id=client.creator_id,
            scopes=frozenset(client.scopes),
        )


def require_api_scope(principal: ScopedPrincipal, scope: str) -> None:
    if scope not in principal.scopes:
        raise MissingAPIScopeError(f"delegated client lacks required scope: {scope}")
