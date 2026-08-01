from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.session import AsyncSessionFactory
from app.models.integration import IntegrationStatus, OAuthDelegation, OAuthIdentity
from app.models.user import User

MAX_BEARER_TOKEN_BYTES = 16_384
MAX_INTROSPECTION_RESPONSE_BYTES = 65_536
MAX_ID_LENGTH = 2_048
MAX_SCOPE_LENGTH = 200
MCP_ACCESS_SCOPE = "mcp:access"


class MissingOAuthPrincipalContextError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OAuthPrincipal:
    identity_id: UUID
    delegation_id: UUID
    user_id: UUID
    client_id: str
    issuer: str
    subject: str
    authorization_id: str
    scopes: frozenset[str]
    consent_version: int

    @property
    def creator_id(self) -> UUID:
        return self.user_id

    @property
    def oauth_client_id(self) -> str:
        return self.client_id


@dataclass(frozen=True, slots=True)
class _IntrospectionClaims:
    issuer: str
    subject: str
    client_id: str
    authorization_id: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int


_oauth_principal_override: ContextVar[OAuthPrincipal | None] = ContextVar(
    "oauth_principal_override",
    default=None,
)


def get_current_oauth_principal() -> OAuthPrincipal:
    """Return the verified local OAuth actor for the current MCP request."""

    override = _oauth_principal_override.get()
    if override is not None:
        return override

    access_token = get_access_token()
    if access_token is None:
        raise MissingOAuthPrincipalContextError("authenticated OAuth context is required")

    claims = access_token.claims or {}
    try:
        identity_id = UUID(_required_claim_string(claims, "oauth_identity_id"))
        delegation_id = UUID(_required_claim_string(claims, "oauth_delegation_id"))
        user_id = UUID(_required_claim_string(claims, "user_id"))
        issuer = _required_claim_string(claims, "iss")
        authorization_id = _required_claim_string(claims, "oauth_authorization_id")
        consent_version = _positive_int(claims.get("oauth_consent_version"))
    except (TypeError, ValueError) as exc:
        raise MissingOAuthPrincipalContextError("verified OAuth principal is malformed") from exc

    if access_token.subject is None or not access_token.subject.strip():
        raise MissingOAuthPrincipalContextError("verified OAuth subject is missing")
    if not access_token.client_id.strip():
        raise MissingOAuthPrincipalContextError("verified OAuth client is missing")

    return OAuthPrincipal(
        identity_id=identity_id,
        delegation_id=delegation_id,
        user_id=user_id,
        client_id=access_token.client_id,
        issuer=issuer,
        subject=access_token.subject,
        authorization_id=authorization_id,
        scopes=frozenset(access_token.scopes),
        consent_version=consent_version,
    )


@contextmanager
def use_oauth_principal(principal: OAuthPrincipal) -> Iterator[None]:
    """Override the MCP OAuth principal in direct, in-process tool tests."""

    token = _oauth_principal_override.set(principal)
    try:
        yield
    finally:
        _oauth_principal_override.reset(token)


class RejectAllTokenVerifier:
    """Keep the MCP endpoint protected when introspection is not configured."""

    async def verify_token(self, token: str) -> AccessToken | None:
        return None

    async def aclose(self) -> None:
        return None


class OAuthIntrospectionTokenVerifier:
    """Validate opaque OAuth tokens with RFC 7662 and local user consent."""

    def __init__(
        self,
        *,
        introspection_url: str,
        introspection_client_id: str,
        introspection_client_secret: str,
        issuer: str,
        resource: str,
        timeout_seconds: float = 5.0,
        clock_skew_seconds: int = 30,
        max_token_lifetime_seconds: int = 3_600,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionFactory,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        identifiers = (introspection_url, introspection_client_id, issuer, resource)
        if not all(value and value == value.strip() for value in identifiers):
            raise ValueError("OAuth introspection settings cannot be blank or padded")
        if not introspection_client_secret:
            raise ValueError("OAuth introspection client secret cannot be blank")
        if timeout_seconds <= 0:
            raise ValueError("OAuth introspection timeout must be positive")
        if not 0 <= clock_skew_seconds <= 300:
            raise ValueError("OAuth clock skew must be between 0 and 300 seconds")
        if max_token_lifetime_seconds <= 0:
            raise ValueError("OAuth maximum token lifetime must be positive")

        self._introspection_url = introspection_url
        self._client_id = introspection_client_id
        self._client_secret = introspection_client_secret
        self._issuer = issuer
        self._resource = resource
        self._timeout = httpx.Timeout(timeout_seconds)
        self._clock_skew_seconds = clock_skew_seconds
        self._max_token_lifetime_seconds = max_token_lifetime_seconds
        self._session_factory = session_factory
        self._transport = transport
        self._clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionFactory,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> OAuthIntrospectionTokenVerifier:
        if (
            settings.mcp_oauth_introspection_url is None
            or settings.mcp_oauth_introspection_client_id is None
            or settings.mcp_oauth_introspection_client_secret is None
        ):
            raise ValueError("OAuth introspection is not configured")
        return cls(
            introspection_url=str(settings.mcp_oauth_introspection_url),
            introspection_client_id=settings.mcp_oauth_introspection_client_id,
            introspection_client_secret=(
                settings.mcp_oauth_introspection_client_secret.get_secret_value()
            ),
            issuer=str(settings.mcp_oauth_issuer_url),
            resource=str(settings.mcp_public_url),
            timeout_seconds=settings.mcp_oauth_introspection_timeout_seconds,
            clock_skew_seconds=settings.mcp_oauth_clock_skew_seconds,
            max_token_lifetime_seconds=settings.mcp_oauth_max_token_lifetime_seconds,
            session_factory=session_factory,
            transport=transport,
            clock=clock,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            token_size = len(token.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        if not token or token != token.strip() or token_size > MAX_BEARER_TOKEN_BYTES:
            return None

        try:
            payload = await self._introspect(token)
            claims = _validate_introspection_payload(
                payload,
                expected_issuer=self._issuer,
                expected_resource=self._resource,
                now=int(self._clock()),
                clock_skew_seconds=self._clock_skew_seconds,
                max_token_lifetime_seconds=self._max_token_lifetime_seconds,
            )
            if claims is None:
                return None
            principal = await _load_oauth_principal(
                claims,
                session_factory=self._session_factory,
                used_at=datetime.fromtimestamp(self._clock(), UTC),
            )
        except (httpx.HTTPError, json.JSONDecodeError, SQLAlchemyError, ValueError, TypeError):
            return None

        if principal is None:
            return None
        return AccessToken(
            # The SDK retains this only in request memory; it is never copied into
            # local claims, logs, or the database.
            token=token,
            client_id=principal.client_id,
            scopes=sorted(principal.scopes),
            expires_at=claims.expires_at,
            resource=self._resource,
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

    async def aclose(self) -> None:
        # Clients are request-scoped so this keeps lifecycle integration uniform
        # without retaining the introspection credential in another object.
        return None

    async def _introspect(self, token: str) -> Mapping[str, Any]:
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(self._client_id, self._client_secret),
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                self._introspection_url,
                data={"token": token, "token_type_hint": "access_token"},
                headers={"Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise ValueError("OAuth introspection failed")
                content_type = response.headers.get("content-type", "")
                if content_type.partition(";")[0].strip().lower() != "application/json":
                    raise ValueError("OAuth introspection did not return JSON")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_INTROSPECTION_RESPONSE_BYTES:
                        raise ValueError("OAuth introspection response is too large")

        payload = json.loads(body, object_pairs_hook=_unique_json_object)
        if not isinstance(payload, dict):
            raise ValueError("OAuth introspection response must be an object")
        return payload


def token_verifier_from_settings(settings: Settings) -> TokenVerifier:
    if not settings.mcp_oauth_introspection_configured:
        return RejectAllTokenVerifier()
    return OAuthIntrospectionTokenVerifier.from_settings(settings)


build_oauth_token_verifier = token_verifier_from_settings


async def _load_oauth_principal(
    claims: _IntrospectionClaims,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    used_at: datetime,
) -> OAuthPrincipal | None:
    async with session_factory() as session:
        result = await session.execute(
            select(OAuthIdentity, OAuthDelegation, User)
            .join(OAuthDelegation, OAuthDelegation.identity_id == OAuthIdentity.id)
            .join(User, User.id == OAuthIdentity.user_id)
            .where(
                OAuthIdentity.issuer == claims.issuer,
                OAuthIdentity.subject == claims.subject,
                OAuthIdentity.status == IntegrationStatus.ACTIVE,
                OAuthDelegation.oauth_client_id == claims.client_id,
                OAuthDelegation.authorization_id == claims.authorization_id,
                OAuthDelegation.status == IntegrationStatus.ACTIVE,
                OAuthDelegation.revoked_at.is_(None),
                User.can_create_tasks.is_(True),
            )
        )
        row = result.one_or_none()
        if row is None:
            return None

        identity, delegation, user = row
        approved_scopes = frozenset(delegation.approved_scopes)
        effective_scopes = claims.scopes & approved_scopes
        identity.last_seen_at = used_at
        delegation.last_used_at = used_at
        await session.commit()
        return OAuthPrincipal(
            identity_id=identity.id,
            delegation_id=delegation.id,
            user_id=user.id,
            client_id=delegation.oauth_client_id,
            issuer=identity.issuer,
            subject=identity.subject,
            authorization_id=delegation.authorization_id,
            scopes=effective_scopes,
            consent_version=delegation.consent_version,
        )


def _validate_introspection_payload(
    payload: Mapping[str, Any],
    *,
    expected_issuer: str,
    expected_resource: str,
    now: int,
    clock_skew_seconds: int,
    max_token_lifetime_seconds: int,
) -> _IntrospectionClaims | None:
    if payload.get("active") is not True:
        return None

    issuer = payload.get("iss")
    if issuer is not None and issuer != expected_issuer:
        return None
    if not _has_exact_resource(payload, expected_resource):
        return None

    subject = _clean_identifier(payload.get("sub"))
    client_id = _clean_identifier(payload.get("client_id"))
    authorization_id = _clean_identifier(payload.get("authorization_id"))
    if subject is None or client_id is None or authorization_id is None:
        return None

    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        return None

    expires_at = _numeric_date(payload.get("exp"))
    if expires_at is None or expires_at <= now:
        return None
    if expires_at - now > max_token_lifetime_seconds + clock_skew_seconds:
        return None

    valid_not_before, not_before = _optional_numeric_date(payload, "nbf")
    issued_at = _numeric_date(payload.get("iat"))
    if not valid_not_before or issued_at is None:
        return None
    if not_before is not None and not_before > now + clock_skew_seconds:
        return None
    if issued_at > now + clock_skew_seconds or expires_at <= issued_at:
        return None
    if expires_at - issued_at > max_token_lifetime_seconds + (2 * clock_skew_seconds):
        return None

    scopes = _parse_scopes(payload)
    if scopes is None:
        return None
    return _IntrospectionClaims(
        issuer=expected_issuer,
        subject=subject,
        client_id=client_id,
        authorization_id=authorization_id,
        scopes=scopes,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _has_exact_resource(payload: Mapping[str, Any], expected_resource: str) -> bool:
    audience = payload.get("aud")
    resource = payload.get("resource")
    if audience is None and resource is None:
        return False

    if audience is not None:
        if isinstance(audience, str):
            audiences = [audience]
        elif isinstance(audience, list) and audience:
            audiences = audience
        else:
            return False
        if any(not isinstance(item, str) or not item for item in audiences):
            return False
        if expected_resource not in audiences:
            return False

    if resource is not None and resource != expected_resource:
        return False
    return True


def _parse_scopes(payload: Mapping[str, Any]) -> frozenset[str] | None:
    value = payload.get("scope", "")
    if not isinstance(value, str) or len(value) > 4_096:
        return None
    if not value:
        return frozenset()
    scopes = value.split(" ")
    if any(
        not scope or len(scope) > MAX_SCOPE_LENGTH or not _is_rfc_scope_token(scope)
        for scope in scopes
    ):
        return None
    return frozenset(scopes)


def _is_rfc_scope_token(value: str) -> bool:
    return all(
        code == 0x21 or 0x23 <= code <= 0x5B or 0x5D <= code <= 0x7E for code in map(ord, value)
    )


def _optional_numeric_date(payload: Mapping[str, Any], key: str) -> tuple[bool, int | None]:
    if key not in payload:
        return True, None
    parsed = _numeric_date(payload[key])
    return parsed is not None, parsed


def _numeric_date(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _clean_identifier(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ID_LENGTH
    ):
        return None
    return value


def _required_claim_string(claims: Mapping[str, Any], key: str) -> str:
    value = _clean_identifier(claims.get(key))
    if value is None:
        raise ValueError(f"missing claim: {key}")
    return value


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("consent version must be positive")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("OAuth introspection response contains a duplicate field")
        result[key] = value
    return result
