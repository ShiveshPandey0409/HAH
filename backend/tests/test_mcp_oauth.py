from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from sqlalchemy import select

from app.db.session import AsyncSessionFactory
from app.mcp import oauth
from app.mcp.oauth import (
    OAuthIntrospectionTokenVerifier,
    OAuthPrincipal,
    _IntrospectionClaims,
    _load_oauth_principal,
    _validate_introspection_payload,
    get_current_oauth_principal,
    use_oauth_principal,
)
from app.models.integration import IntegrationStatus, OAuthDelegation, OAuthIdentity
from app.models.user import User

NOW = 2_000_000_000
ISSUER = "https://auth.example.com/"
RESOURCE = "https://api.example.com/mcp"
CLIENT_ID = "agent-client"
AUTHORIZATION_ID = "authorization-grant-123"
TOKEN = "opaque-access-token-never-persist"


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "active": True,
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "external-user-123",
        "client_id": CLIENT_ID,
        "authorization_id": AUTHORIZATION_ID,
        "token_type": "Bearer",
        "scope": "mcp:access tasks:create submissions:verify",
        "iat": NOW - 60,
        "nbf": NOW - 60,
        "exp": NOW + 300,
    }
    payload.update(overrides)
    return payload


def validate(payload: dict[str, object]) -> _IntrospectionClaims | None:
    return _validate_introspection_payload(
        payload,
        expected_issuer=ISSUER,
        expected_resource=RESOURCE,
        now=NOW,
        clock_skew_seconds=30,
        max_token_lifetime_seconds=3_600,
    )


def invalid_payloads() -> list[object]:
    missing_audience = valid_payload()
    missing_audience.pop("aud")
    missing_expiry = valid_payload()
    missing_expiry.pop("exp")
    missing_issued_at = valid_payload()
    missing_issued_at.pop("iat")
    missing_authorization_id = valid_payload()
    missing_authorization_id.pop("authorization_id")
    return [
        pytest.param(valid_payload(active=False), id="inactive"),
        pytest.param(valid_payload(iss="https://other.example.com/"), id="wrong-issuer"),
        pytest.param(valid_payload(aud=f"{RESOURCE}/other"), id="wrong-audience"),
        pytest.param(valid_payload(resource="https://other.example.com/mcp"), id="wrong-resource"),
        pytest.param(missing_audience, id="missing-audience-or-resource"),
        pytest.param(missing_expiry, id="missing-expiry"),
        pytest.param(missing_issued_at, id="missing-issued-at"),
        pytest.param(missing_authorization_id, id="missing-authorization-id"),
        pytest.param(valid_payload(exp=NOW), id="expired"),
        pytest.param(valid_payload(exp=NOW + 3_631), id="excessive-remaining-lifetime"),
        pytest.param(valid_payload(nbf=NOW + 31), id="not-yet-valid"),
        pytest.param(valid_payload(iat=NOW + 31), id="future-issued-at"),
        pytest.param(valid_payload(sub=""), id="missing-subject"),
        pytest.param(valid_payload(client_id=" padded "), id="padded-client"),
        pytest.param(valid_payload(authorization_id=" padded "), id="padded-authorization-id"),
        pytest.param(valid_payload(token_type="DPoP"), id="wrong-token-type"),
        pytest.param(valid_payload(scope=["tasks:create"]), id="nonstandard-scope-shape"),
        pytest.param(valid_payload(scope="mcp:access\ttasks:create"), id="invalid-scope-separator"),
        pytest.param(valid_payload(exp=True), id="boolean-numeric-date"),
    ]


@pytest.mark.parametrize("payload", invalid_payloads())
def test_introspection_claim_validation_fails_closed(payload: dict[str, object]) -> None:
    assert validate(payload) is None


def test_introspection_claim_validation_accepts_exact_multi_audience_and_optional_issuer() -> None:
    payload = valid_payload(
        aud=["https://another-resource.example", RESOURCE],
        resource=RESOURCE,
        scope="tasks:create tasks:create mcp:access",
    )
    payload.pop("iss")

    claims = validate(payload)

    assert claims is not None
    assert claims.issuer == ISSUER
    assert claims.subject == "external-user-123"
    assert claims.client_id == CLIENT_ID
    assert claims.authorization_id == AUTHORIZATION_ID
    assert claims.scopes == frozenset({"tasks:create", "mcp:access"})
    assert claims.issued_at == NOW - 60
    assert claims.expires_at == NOW + 300


async def test_verifier_uses_basic_authenticated_post_and_returns_safe_local_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = OAuthPrincipal(
        identity_id=uuid4(),
        delegation_id=uuid4(),
        user_id=uuid4(),
        client_id=CLIENT_ID,
        issuer=ISSUER,
        subject="external-user-123",
        authorization_id=AUTHORIZATION_ID,
        scopes=frozenset({"mcp:access", "tasks:create"}),
        consent_version=3,
    )
    loaded_claims: list[_IntrospectionClaims] = []

    async def load_principal(
        claims: _IntrospectionClaims,
        **_: object,
    ) -> OAuthPrincipal:
        loaded_claims.append(claims)
        return principal

    monkeypatch.setattr(oauth, "_load_oauth_principal", load_principal)

    async def introspection(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://auth.example.com/introspect"
        expected_auth = base64.b64encode(b"resource-server:secret").decode()
        assert request.headers["authorization"] == f"Basic {expected_auth}"
        assert request.headers["accept"] == "application/json"
        form = parse_qs(request.content.decode())
        assert form == {"token": [TOKEN], "token_type_hint": ["access_token"]}
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json=valid_payload(),
        )

    verifier = OAuthIntrospectionTokenVerifier(
        introspection_url="https://auth.example.com/introspect",
        introspection_client_id="resource-server",
        introspection_client_secret="secret",
        issuer=ISSUER,
        resource=RESOURCE,
        transport=httpx.MockTransport(introspection),
        clock=lambda: NOW,
    )

    access_token = await verifier.verify_token(TOKEN)
    await verifier.aclose()

    assert access_token is not None
    assert access_token.token == TOKEN
    assert access_token.client_id == CLIENT_ID
    assert access_token.subject == principal.subject
    assert access_token.resource == RESOURCE
    assert access_token.scopes == ["mcp:access", "tasks:create"]
    assert access_token.claims == {
        "iss": ISSUER,
        "oauth_identity_id": str(principal.identity_id),
        "oauth_delegation_id": str(principal.delegation_id),
        "oauth_authorization_id": AUTHORIZATION_ID,
        "user_id": str(principal.user_id),
        "oauth_consent_version": 3,
    }
    assert TOKEN not in str(access_token.claims)
    assert loaded_claims[0].scopes == frozenset(
        {"mcp:access", "tasks:create", "submissions:verify"}
    )


@pytest.mark.parametrize(
    ("response", "presented_token"),
    [
        (httpx.Response(401, json={"error": "invalid_client"}), TOKEN),
        (
            httpx.Response(200, headers={"content-type": "text/html"}, text="no"),
            TOKEN,
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"active":true,"active":false}',
            ),
            TOKEN,
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"x" * (oauth.MAX_INTROSPECTION_RESPONSE_BYTES + 1),
            ),
            TOKEN,
        ),
        (httpx.Response(200, json=valid_payload()), "\ud800"),
    ],
    ids=["non-200", "non-json", "duplicate-json-field", "oversized", "invalid-unicode"],
)
async def test_verifier_rejects_bad_transport_responses_without_mapping(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    presented_token: str,
) -> None:
    mapped = False

    async def load_principal(*_: object, **__: object) -> OAuthPrincipal | None:
        nonlocal mapped
        mapped = True
        return None

    monkeypatch.setattr(oauth, "_load_oauth_principal", load_principal)
    verifier = OAuthIntrospectionTokenVerifier(
        introspection_url="https://auth.example.com/introspect",
        introspection_client_id="resource-server",
        introspection_client_secret="secret",
        issuer=ISSUER,
        resource=RESOURCE,
        transport=httpx.MockTransport(lambda _: response),
        clock=lambda: NOW,
    )

    assert await verifier.verify_token(presented_token) is None
    assert mapped is False


async def test_verifier_rejects_introspection_network_failure() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    verifier = OAuthIntrospectionTokenVerifier(
        introspection_url="https://auth.example.com/introspect",
        introspection_client_id="resource-server",
        introspection_client_secret="secret",
        issuer=ISSUER,
        resource=RESOURCE,
        transport=httpx.MockTransport(fail),
        clock=lambda: NOW,
    )

    assert await verifier.verify_token(TOKEN) is None


async def test_local_mapping_is_exact_intersects_scopes_and_updates_usage() -> None:
    async with AsyncSessionFactory() as session:
        user = User(
            email="oauth-creator@example.com",
            display_name="OAuth Creator",
            can_create_tasks=True,
            can_work_tasks=False,
        )
        session.add(user)
        await session.flush()
        identity = OAuthIdentity(
            user_id=user.id,
            issuer=ISSUER,
            subject="external-user-123",
            status=IntegrationStatus.ACTIVE,
        )
        session.add(identity)
        await session.flush()
        delegation = OAuthDelegation(
            identity_id=identity.id,
            oauth_client_id=CLIENT_ID,
            approved_scopes=["mcp:access", "tasks:create"],
            status=IntegrationStatus.ACTIVE,
            consent_version=4,
            authorization_id=AUTHORIZATION_ID,
        )
        session.add(delegation)
        await session.commit()
        identity_id = identity.id
        delegation_id = delegation.id
        user_id = user.id

    claims = _IntrospectionClaims(
        issuer=ISSUER,
        subject="external-user-123",
        client_id=CLIENT_ID,
        authorization_id=AUTHORIZATION_ID,
        scopes=frozenset({"mcp:access", "tasks:create", "submissions:verify"}),
        issued_at=NOW - 60,
        expires_at=NOW + 300,
    )
    used_at = datetime.fromtimestamp(NOW, UTC)

    assert (
        await _load_oauth_principal(
            _IntrospectionClaims(
                issuer=f"{ISSUER}other",
                subject=claims.subject,
                client_id=claims.client_id,
                authorization_id=claims.authorization_id,
                scopes=claims.scopes,
                issued_at=claims.issued_at,
                expires_at=claims.expires_at,
            ),
            session_factory=AsyncSessionFactory,
            used_at=used_at,
        )
        is None
    )
    principal = await _load_oauth_principal(
        claims,
        session_factory=AsyncSessionFactory,
        used_at=used_at,
    )

    assert principal == OAuthPrincipal(
        identity_id=identity_id,
        delegation_id=delegation_id,
        user_id=user_id,
        client_id=CLIENT_ID,
        issuer=ISSUER,
        subject="external-user-123",
        authorization_id=AUTHORIZATION_ID,
        scopes=frozenset({"mcp:access", "tasks:create"}),
        consent_version=4,
    )
    async with AsyncSessionFactory() as session:
        stored_identity = await session.get(OAuthIdentity, identity_id)
        stored_delegation = await session.get(OAuthDelegation, delegation_id)
        assert stored_identity is not None
        assert stored_delegation is not None
        assert stored_identity.last_seen_at == used_at
        assert stored_delegation.last_used_at == used_at


async def test_local_mapping_rejects_disabled_or_non_creator_actors() -> None:
    async with AsyncSessionFactory() as session:
        user = User(
            email="oauth-worker@example.com",
            display_name="OAuth Worker",
            can_create_tasks=False,
            can_work_tasks=True,
        )
        session.add(user)
        await session.flush()
        identity = OAuthIdentity(
            user_id=user.id,
            issuer=ISSUER,
            subject="worker-subject",
            status=IntegrationStatus.ACTIVE,
        )
        session.add(identity)
        await session.flush()
        session.add(
            OAuthDelegation(
                identity_id=identity.id,
                oauth_client_id=CLIENT_ID,
                approved_scopes=["mcp:access"],
                status=IntegrationStatus.ACTIVE,
                consent_version=1,
                authorization_id=AUTHORIZATION_ID,
            )
        )
        await session.commit()

    assert (
        await _load_oauth_principal(
            _IntrospectionClaims(
                issuer=ISSUER,
                subject="worker-subject",
                client_id=CLIENT_ID,
                authorization_id=AUTHORIZATION_ID,
                scopes=frozenset({"mcp:access"}),
                issued_at=NOW - 60,
                expires_at=NOW + 300,
            ),
            session_factory=AsyncSessionFactory,
            used_at=datetime.fromtimestamp(NOW, UTC),
        )
        is None
    )


async def test_local_mapping_rejects_tokens_from_a_superseded_authorization_grant() -> None:
    used_at = datetime.fromtimestamp(NOW, UTC)
    async with AsyncSessionFactory() as session:
        user = User(
            email="oauth-consent-grant@example.com",
            display_name="OAuth Consent Grant",
            can_create_tasks=True,
            can_work_tasks=False,
        )
        session.add(user)
        await session.flush()
        identity = OAuthIdentity(
            user_id=user.id,
            issuer=ISSUER,
            subject="consent-grant-subject",
            status=IntegrationStatus.ACTIVE,
        )
        session.add(identity)
        await session.flush()
        session.add(
            OAuthDelegation(
                identity_id=identity.id,
                oauth_client_id=CLIENT_ID,
                approved_scopes=["mcp:access"],
                status=IntegrationStatus.ACTIVE,
                consent_version=2,
                authorization_id="current-authorization-grant",
            )
        )
        await session.commit()

    def grant_claims(authorization_id: str) -> _IntrospectionClaims:
        return _IntrospectionClaims(
            issuer=ISSUER,
            subject="consent-grant-subject",
            client_id=CLIENT_ID,
            authorization_id=authorization_id,
            scopes=frozenset({"mcp:access"}),
            issued_at=NOW - 60,
            expires_at=NOW + 300,
        )

    assert (
        await _load_oauth_principal(
            grant_claims("superseded-authorization-grant"),
            session_factory=AsyncSessionFactory,
            used_at=used_at,
        )
        is None
    )
    assert (
        await _load_oauth_principal(
            grant_claims("current-authorization-grant"),
            session_factory=AsyncSessionFactory,
            used_at=used_at,
        )
        is not None
    )


def test_current_principal_supports_sdk_context_and_scoped_test_override() -> None:
    principal = OAuthPrincipal(
        identity_id=uuid4(),
        delegation_id=uuid4(),
        user_id=uuid4(),
        client_id=CLIENT_ID,
        issuer=ISSUER,
        subject="external-user-123",
        authorization_id=AUTHORIZATION_ID,
        scopes=frozenset({"mcp:access"}),
        consent_version=2,
    )
    token = AccessToken(
        token=TOKEN,
        client_id=principal.client_id,
        scopes=sorted(principal.scopes),
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
    context_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        assert get_current_oauth_principal() == principal
        override = OAuthPrincipal(
            identity_id=uuid4(),
            delegation_id=uuid4(),
            user_id=uuid4(),
            client_id="test-client",
            issuer=ISSUER,
            subject="test-subject",
            authorization_id="test-authorization-grant",
            scopes=frozenset(),
            consent_version=1,
        )
        with use_oauth_principal(override):
            assert get_current_oauth_principal() == override
        assert get_current_oauth_principal() == principal
    finally:
        auth_context_var.reset(context_token)


async def test_no_raw_bearer_is_written_to_oauth_records() -> None:
    async with AsyncSessionFactory() as session:
        user = User(
            email="oauth-token-secrecy@example.com",
            display_name="OAuth Token Secrecy",
            can_create_tasks=True,
            can_work_tasks=False,
        )
        session.add(user)
        await session.flush()
        identity = OAuthIdentity(
            user_id=user.id,
            issuer=ISSUER,
            subject="external-user-123",
            status=IntegrationStatus.ACTIVE,
        )
        session.add(identity)
        await session.flush()
        session.add(
            OAuthDelegation(
                identity_id=identity.id,
                oauth_client_id=CLIENT_ID,
                authorization_id=AUTHORIZATION_ID,
                approved_scopes=["mcp:access", "tasks:create", "submissions:verify"],
                status=IntegrationStatus.ACTIVE,
                consent_version=1,
            )
        )
        await session.commit()

    async def introspection(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=valid_payload())

    verifier = OAuthIntrospectionTokenVerifier(
        introspection_url="https://auth.example.com/introspect",
        introspection_client_id="resource-server",
        introspection_client_secret="secret",
        issuer=ISSUER,
        resource=RESOURCE,
        transport=httpx.MockTransport(introspection),
        clock=lambda: NOW,
    )
    assert await verifier.verify_token(TOKEN) is not None
    async with AsyncSessionFactory() as session:
        identities = (await session.scalars(select(OAuthIdentity))).all()
        delegations = (await session.scalars(select(OAuthDelegation))).all()
        assert all(TOKEN not in repr(item.__dict__) for item in [*identities, *delegations])
