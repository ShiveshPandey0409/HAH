from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.db.session import AsyncSessionFactory
from app.main import app
from app.models.integration import (
    OAuthDelegation,
    OAuthIdentity,
    OAuthIssuedToken,
    OAuthRegisteredClient,
)

PASSWORD = "correct horse battery staple"
REDIRECT_URI = "http://127.0.0.1:19191/callback"
SCOPES = "mcp:access tasks:create payments:read payments:write"
VERIFIER = "oauth-pkce-verifier-abcdefghijklmnopqrstuvwxyz-0123456789-ABCDE"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)


async def _register(
    client: AsyncClient,
    *,
    redirect_uri: str = REDIRECT_URI,
    client_name: str = "OAuth integration test",
) -> dict[str, object]:
    response = await client.post(
        "/register",
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "scope": SCOPES,
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _authorize(
    client: AsyncClient,
    client_id: str,
    *,
    state: str,
    redirect_uri: str = REDIRECT_URI,
) -> str:
    response = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": state,
            "scope": SCOPES,
            "resource": "http://127.0.0.1:8000/mcp",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["request"][0]


async def test_metadata_registration_and_full_pkce_token_lifecycle(
    client: AsyncClient,
    monkeypatch,
) -> None:
    metadata = await client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == "http://localhost:9000/"
    assert metadata.json()["authorization_endpoint"] == "http://localhost:9000/authorize"
    assert metadata.json()["token_endpoint"] == "http://localhost:9000/token"
    assert metadata.json()["registration_endpoint"] == "http://localhost:9000/register"
    assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
    assert metadata.json()["authorization_response_iss_parameter_supported"] is False

    signup = await client.post(
        "/v1/auth/signup",
        json={
            "email": "oauth-creator@example.com",
            "password": PASSWORD,
            "display_name": "OAuth Creator",
            "can_create_tasks": True,
            "can_work_tasks": False,
        },
    )
    assert signup.status_code == 201

    registration = await _register(client)
    client_id = str(registration["client_id"])
    client_secret = str(registration["client_secret"])
    assert client_secret

    async with AsyncSessionFactory() as session:
        stored_client = await session.get(OAuthRegisteredClient, client_id)
    assert stored_client is not None
    assert stored_client.client_secret_hash is not None
    assert stored_client.client_secret_hash != client_secret
    assert stored_client.client_secret_ciphertext is not None
    assert client_secret.encode() not in stored_client.client_secret_ciphertext
    assert "client_secret" not in stored_client.client_metadata

    denied_handle = await _authorize(client, client_id, state="state-denied")
    denied = await client.post(
        "/oauth/consent",
        data={"request": denied_handle, "action": "deny"},
        follow_redirects=False,
    )
    assert denied.status_code == 302
    denied_query = parse_qs(urlparse(denied.headers["location"]).query)
    assert denied_query["error"] == ["access_denied"]
    assert denied_query["state"] == ["state-denied"]
    assert denied_query["iss"] == ["http://localhost:9000/"]

    request_handle = await _authorize(client, client_id, state="state-one")
    consent = await client.get("/oauth/consent", params={"request": request_handle})
    assert consent.status_code == 200
    assert "OAuth integration test" in consent.text
    assert "Create campaign tasks" in consent.text
    assert "Read sandbox balances" in consent.text
    assert "Manage sandbox funding" in consent.text
    assert "Use the same creator account as your HAH dashboard" in consent.text
    assert "Use the same HAH account" in consent.text
    assert "Allow MCP access once" in consent.text
    assert client_secret not in consent.text

    wrong_password = await client.post(
        "/oauth/consent",
        data={
            "request": request_handle,
            "email": "oauth-creator@example.com",
            "password": "incorrect",
            "action": "approve",
        },
        follow_redirects=False,
    )
    assert wrong_password.status_code == 401

    approved = await client.post(
        "/oauth/consent",
        data={
            "request": request_handle,
            "email": "oauth-creator@example.com",
            "password": PASSWORD,
            "action": "approve",
        },
        follow_redirects=False,
    )
    assert approved.status_code == 302, approved.text
    callback = urlparse(approved.headers["location"])
    callback_query = parse_qs(callback.query)
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT_URI
    assert callback_query["state"] == ["state-one"]
    assert callback_query["iss"] == ["http://localhost:9000/"]
    authorization_code = callback_query["code"][0]

    exchanged = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": VERIFIER,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    tokens = exchanged.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    assert access_token.startswith("hah_oauth_at_")
    assert refresh_token.startswith("hah_oauth_rt_")

    replay = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": VERIFIER,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    async with AsyncSessionFactory() as session:
        issued = (await session.scalars(select(OAuthIssuedToken))).all()
    assert len(issued) == 2
    assert all(access_token not in row.token_hash for row in issued)
    assert all(refresh_token not in row.token_hash for row in issued)

    provider = app.state.oauth_provider
    monkeypatch.setattr(
        provider.settings,
        "mcp_oauth_introspection_client_id",
        "hah-mcp-resource-server",
    )
    monkeypatch.setattr(
        provider.settings,
        "mcp_oauth_introspection_client_secret",
        SecretStr("introspection-test-secret"),
    )
    introspection_auth = ("hah-mcp-resource-server", "introspection-test-secret")

    assert (await client.post("/oauth/introspect", data={"token": access_token})).status_code == 401
    active = await client.post(
        "/oauth/introspect",
        data={"token": access_token},
        auth=introspection_auth,
    )
    assert active.status_code == 200
    claims = active.json()
    assert claims["active"] is True
    assert claims["client_id"] == client_id
    assert claims["sub"].startswith("hah-user:")
    assert claims["aud"] == "http://127.0.0.1:8000/mcp"
    assert claims["authorization_id"].startswith("hah_oauth_grant_")

    refreshed = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new_access = refreshed.json()["access_token"]
    new_refresh = refreshed.json()["refresh_token"]
    assert new_access != access_token
    assert new_refresh != refresh_token

    old_access = await client.post(
        "/oauth/introspect",
        data={"token": access_token},
        auth=introspection_auth,
    )
    assert old_access.json() == {"active": False}
    current_access = await client.post(
        "/oauth/introspect",
        data={"token": new_access},
        auth=introspection_auth,
    )
    assert current_access.json()["active"] is True

    revoked = await client.post(
        "/revoke",
        data={
            "token": new_refresh,
            "token_type_hint": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert revoked.status_code == 200
    after_revoke = await client.post(
        "/oauth/introspect",
        data={"token": new_access},
        auth=introspection_auth,
    )
    assert after_revoke.json() == {"active": False}


async def test_pkce_target_and_consent_fail_closed(client: AsyncClient) -> None:
    registration = await _register(client)
    client_id = str(registration["client_id"])

    bad_pkce = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": "not-s256",
            "code_challenge_method": "S256",
            "scope": "mcp:access",
        },
        follow_redirects=False,
    )
    assert bad_pkce.status_code == 302
    assert parse_qs(urlparse(bad_pkce.headers["location"]).query)["error"] == ["invalid_request"]

    bad_target = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "scope": "mcp:access",
            "resource": "https://attacker.example/mcp",
        },
        follow_redirects=False,
    )
    assert bad_target.status_code == 302
    assert parse_qs(urlparse(bad_target.headers["location"]).query)["error"] == ["invalid_target"]

    invalid_consent = await client.get(
        "/oauth/consent",
        params={"request": "hah_oauth_request_invalid"},
    )
    assert invalid_consent.status_code == 400
    assert invalid_consent.headers["cache-control"] == "no-store"
    assert "Start a new connection" in invalid_consent.text
    assert "same HAH account on every computer" in invalid_consent.text


async def test_same_hah_account_can_connect_from_two_computers(
    client: AsyncClient,
) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "email": "oauth-multi-device@example.com",
            "password": PASSWORD,
            "display_name": "Multi-device Creator",
            "can_create_tasks": True,
            "can_work_tasks": False,
        },
    )
    assert signup.status_code == 201

    connections = (
        ("Laptop", "http://127.0.0.1:19191/callback", "laptop-state"),
        ("Desktop", "http://127.0.0.1:29292/callback", "desktop-state"),
    )
    client_ids: set[str] = set()
    access_tokens: list[str] = []

    for client_name, redirect_uri, state in connections:
        registration = await _register(
            client,
            redirect_uri=redirect_uri,
            client_name=client_name,
        )
        client_id = str(registration["client_id"])
        client_secret = str(registration["client_secret"])
        client_ids.add(client_id)

        request_handle = await _authorize(
            client,
            client_id,
            state=state,
            redirect_uri=redirect_uri,
        )
        approved = await client.post(
            "/oauth/consent",
            data={
                "request": request_handle,
                "email": "oauth-multi-device@example.com",
                "password": PASSWORD,
                "action": "approve",
            },
            follow_redirects=False,
        )
        assert approved.status_code == 302, approved.text
        callback = urlparse(approved.headers["location"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == redirect_uri
        callback_query = parse_qs(callback.query)
        assert callback_query["state"] == [state]

        exchanged = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": callback_query["code"][0],
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": VERIFIER,
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        access_tokens.append(exchanged.json()["access_token"])

    assert len(client_ids) == 2
    loaded_tokens = [
        await app.state.oauth_provider.load_access_token(access_token)
        for access_token in access_tokens
    ]
    assert all(token is not None for token in loaded_tokens)

    async with AsyncSessionFactory() as session:
        identities = (await session.scalars(select(OAuthIdentity))).all()
        delegations = (await session.scalars(select(OAuthDelegation))).all()
    assert len(identities) == 1
    assert len(delegations) == 2
    assert {delegation.oauth_client_id for delegation in delegations} == client_ids
    assert {delegation.identity_id for delegation in delegations} == {identities[0].id}
