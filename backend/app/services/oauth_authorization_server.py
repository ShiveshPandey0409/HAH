from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlencode
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken, MultiFernet
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.core.config import Settings
from app.models.integration import (
    IntegrationStatus,
    OAuthAuthorizationCode,
    OAuthAuthorizationRequest,
    OAuthDelegation,
    OAuthIdentity,
    OAuthIssuedToken,
    OAuthRegisteredClient,
)
from app.models.user import User
from app.services.auth import authenticate_password_user
from app.services.oauth_delegations import (
    OAUTH_SUPPORTED_SCOPES,
    OAuthDelegationValidationError,
    OAuthIdentityConflictError,
    grant_oauth_delegation,
)
from app.services.webhooks import build_webhook_cipher

AUTHORIZATION_REQUEST_TTL = timedelta(minutes=10)
AUTHORIZATION_CODE_TTL = timedelta(minutes=5)
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
MAX_OAUTH_FIELD_LENGTH = 2_048
_PKCE_S256_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

_SCOPE_DESCRIPTIONS = {
    "mcp:access": ("Connect to HAH", "Use the HAH MCP server as your signed-in account."),
    "tasks:create": ("Create campaign tasks", "Draft tasks and bounties for your review."),
    "submissions:read": ("Read submitted proof", "View URLs and uploaded evidence for your tasks."),
    "submissions:verify": ("Verify submissions", "Run the configured proof checks."),
    "submissions:approve": (
        "Approve completed work",
        "Mark verified work as approved for its reward.",
    ),
    "payments:read": (
        "Read sandbox balances",
        "Check payment status, allowance, and wallet balances.",
    ),
    "payments:write": ("Manage sandbox funding", "Start or refresh task funding approvals."),
}


class StoredAuthorizationCode(AuthorizationCode):
    code_hash: str
    delegation_id: UUID
    authorization_id: str


class StoredRefreshToken(RefreshToken):
    token_hash: str
    delegation_id: UUID
    authorization_id: str
    resource: str
    family_id: UUID
    issued_at: int


class StoredAccessToken(AccessToken):
    token_hash: str
    delegation_id: UUID
    authorization_id: str
    family_id: UUID
    issued_at: int


def _credential_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _new_credential(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _now() -> datetime:
    return datetime.now(UTC)


class FirstPartyOAuthProvider(
    OAuthAuthorizationServerProvider[
        StoredAuthorizationCode,
        StoredRefreshToken,
        StoredAccessToken,
    ]
):
    """Database-backed OAuth 2.0 authorization server for MCP clients."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.issuer = str(settings.mcp_oauth_issuer_url)
        self.resource = str(settings.mcp_public_url)
        self._cipher: MultiFernet | None = None
        if settings.webhook_secret_encryption_keys:
            self._cipher = build_webhook_cipher(settings.webhook_secret_encryption_keys)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id or len(client_id) > MAX_OAUTH_FIELD_LENGTH:
            return None
        async with self.session_factory() as session:
            registered = await session.get(OAuthRegisteredClient, client_id)
        if registered is None:
            return None

        client_secret: str | None = None
        if registered.client_secret_ciphertext is not None:
            if self._cipher is None or registered.client_secret_hash is None:
                return None
            try:
                client_secret = self._cipher.decrypt(registered.client_secret_ciphertext).decode(
                    "utf-8"
                )
            except (InvalidToken, UnicodeError):
                return None
            if not hmac.compare_digest(
                _credential_hash(client_secret),
                registered.client_secret_hash,
            ):
                return None

        return OAuthClientInformationFull.model_validate(
            {
                **registered.client_metadata,
                "client_id": registered.client_id,
                "client_secret": client_secret,
                "client_id_issued_at": registered.client_id_issued_at,
                "client_secret_expires_at": registered.client_secret_expires_at,
            }
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if (
            not client_info.client_id
            or len(client_info.client_id) > MAX_OAUTH_FIELD_LENGTH
            or client_info.token_endpoint_auth_method
            not in {"none", "client_secret_post", "client_secret_basic"}
        ):
            raise RegistrationError(
                "invalid_client_metadata",
                "The requested client authentication method is not supported",
            )
        if client_info.client_secret is not None and self._cipher is None:
            raise RegistrationError(
                "invalid_client_metadata",
                "Confidential client registration is unavailable",
            )

        secret_hash = None
        secret_ciphertext = None
        if client_info.client_secret is not None:
            secret_hash = _credential_hash(client_info.client_secret)
            assert self._cipher is not None
            secret_ciphertext = self._cipher.encrypt(client_info.client_secret.encode("utf-8"))

        metadata = client_info.model_dump(
            mode="json",
            exclude={
                "client_id",
                "client_secret",
                "client_id_issued_at",
                "client_secret_expires_at",
                "issuer",
            },
            exclude_none=True,
        )
        registered = OAuthRegisteredClient(
            client_id=client_info.client_id,
            client_metadata=metadata,
            client_secret_hash=secret_hash,
            client_secret_ciphertext=secret_ciphertext,
            client_id_issued_at=client_info.client_id_issued_at or int(_now().timestamp()),
            client_secret_expires_at=client_info.client_secret_expires_at,
        )
        async with self.session_factory() as session:
            session.add(registered)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise RegistrationError(
                    "invalid_client_metadata",
                    "The OAuth client could not be registered",
                ) from error

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if not _PKCE_S256_RE.fullmatch(params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required")
        if params.state is not None and len(params.state) > MAX_OAUTH_FIELD_LENGTH:
            raise AuthorizeError("invalid_request", "OAuth state is too long")

        resource = params.resource or self.resource
        if resource != self.resource:
            raise AuthorizeError("invalid_target", "The requested resource is not supported")
        scopes = params.scopes
        if scopes is None:
            scopes = (client.scope or "").split()
        scope_set = set(scopes)
        if (
            not scope_set
            or "mcp:access" not in scope_set
            or scope_set - OAUTH_SUPPORTED_SCOPES
            or len(scope_set) != len(scopes)
        ):
            raise AuthorizeError("invalid_scope", "The requested MCP scopes are not supported")

        request_handle = _new_credential("hah_oauth_request_")
        now = _now()
        record = OAuthAuthorizationRequest(
            request_hash=_credential_hash(request_handle),
            client_id=client.client_id,
            state=params.state,
            scopes=sorted(scope_set),
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=resource,
            expires_at=now + AUTHORIZATION_REQUEST_TTL,
        )
        async with self.session_factory() as session:
            session.add(record)
            await session.commit()
        return f"{self.issuer.rstrip('/')}/oauth/consent?{urlencode({'request': request_handle})}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> StoredAuthorizationCode | None:
        if not authorization_code.startswith("hah_oauth_code_") or len(authorization_code) > 256:
            return None
        async with self.session_factory() as session:
            result = await session.execute(
                select(OAuthAuthorizationCode, OAuthIdentity)
                .join(
                    OAuthDelegation,
                    OAuthDelegation.id == OAuthAuthorizationCode.delegation_id,
                )
                .join(OAuthIdentity, OAuthIdentity.id == OAuthDelegation.identity_id)
                .where(
                    OAuthAuthorizationCode.code_hash == _credential_hash(authorization_code),
                    OAuthAuthorizationCode.client_id == client.client_id,
                    OAuthAuthorizationCode.consumed_at.is_(None),
                )
            )
            row = result.one_or_none()
        if row is None:
            return None
        code, identity = row
        return StoredAuthorizationCode(
            code=authorization_code,
            code_hash=code.code_hash,
            delegation_id=code.delegation_id,
            authorization_id=code.authorization_id,
            scopes=code.scopes,
            expires_at=code.expires_at.timestamp(),
            client_id=code.client_id,
            code_challenge=code.code_challenge,
            redirect_uri=code.redirect_uri,
            redirect_uri_provided_explicitly=code.redirect_uri_provided_explicitly,
            resource=code.resource,
            subject=identity.subject,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: StoredAuthorizationCode,
    ) -> OAuthToken:
        now = _now()
        async with self.session_factory() as session:
            code = await session.scalar(
                select(OAuthAuthorizationCode)
                .where(
                    OAuthAuthorizationCode.code_hash == authorization_code.code_hash,
                    OAuthAuthorizationCode.client_id == client.client_id,
                )
                .with_for_update()
            )
            if code is None or code.consumed_at is not None or code.expires_at <= now:
                raise TokenError("invalid_grant", "Authorization code is invalid or expired")
            delegation = await self._active_delegation(
                session,
                code.delegation_id,
                code.authorization_id,
                client.client_id,
            )
            if delegation is None:
                raise TokenError("invalid_grant", "OAuth consent is no longer active")

            code.consumed_at = now
            token = self._issue_token_pair(
                session,
                delegation_id=delegation.id,
                client_id=client.client_id,
                authorization_id=code.authorization_id,
                scopes=code.scopes,
                resource=code.resource,
                family_id=uuid4(),
                now=now,
            )
            await session.commit()
            return token

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> StoredRefreshToken | None:
        record = await self._load_token(
            refresh_token,
            token_kind="refresh",
            client_id=client.client_id,
        )
        if record is None:
            return None
        token, identity = record
        return StoredRefreshToken(
            token=refresh_token,
            token_hash=token.token_hash,
            delegation_id=token.delegation_id,
            authorization_id=token.authorization_id,
            client_id=token.client_id,
            scopes=token.scopes,
            expires_at=int(token.expires_at.timestamp()),
            subject=identity.subject,
            resource=token.resource,
            family_id=token.family_id,
            issued_at=int(token.created_at.timestamp()),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = _now()
        async with self.session_factory() as session:
            current = await session.scalar(
                select(OAuthIssuedToken)
                .where(
                    OAuthIssuedToken.token_hash == refresh_token.token_hash,
                    OAuthIssuedToken.token_kind == "refresh",
                    OAuthIssuedToken.client_id == client.client_id,
                )
                .with_for_update()
            )
            if current is None or current.revoked_at is not None or current.expires_at <= now:
                raise TokenError("invalid_grant", "Refresh token is invalid or expired")
            delegation = await self._active_delegation(
                session,
                current.delegation_id,
                current.authorization_id,
                client.client_id,
            )
            if delegation is None:
                raise TokenError("invalid_grant", "OAuth consent is no longer active")
            if set(scopes) - set(current.scopes):
                raise TokenError("invalid_scope", "Refresh cannot expand the approved scopes")

            await session.execute(
                update(OAuthIssuedToken)
                .where(
                    OAuthIssuedToken.family_id == current.family_id,
                    OAuthIssuedToken.revoked_at.is_(None),
                    OAuthIssuedToken.expires_at >= now,
                )
                .values(revoked_at=now)
            )
            token = self._issue_token_pair(
                session,
                delegation_id=delegation.id,
                client_id=client.client_id,
                authorization_id=current.authorization_id,
                scopes=sorted(set(scopes)),
                resource=current.resource,
                family_id=current.family_id,
                now=now,
            )
            await session.commit()
            return token

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        record = await self._load_token(token, token_kind="access")
        if record is None:
            return None
        issued, identity = record
        return StoredAccessToken(
            token=token,
            token_hash=issued.token_hash,
            delegation_id=issued.delegation_id,
            authorization_id=issued.authorization_id,
            family_id=issued.family_id,
            issued_at=int(issued.created_at.timestamp()),
            client_id=issued.client_id,
            scopes=issued.scopes,
            expires_at=int(issued.expires_at.timestamp()),
            resource=issued.resource,
            subject=identity.subject,
            claims={"iss": self.issuer},
        )

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        now = _now()
        async with self.session_factory() as session:
            await session.execute(
                update(OAuthIssuedToken)
                .where(
                    OAuthIssuedToken.family_id == token.family_id,
                    OAuthIssuedToken.client_id == token.client_id,
                    OAuthIssuedToken.revoked_at.is_(None),
                    OAuthIssuedToken.expires_at >= now,
                )
                .values(revoked_at=now)
            )
            await session.commit()

    async def consent_page(self, request: Request) -> Response:
        request_handle = request.query_params.get("request", "")
        loaded = await self._load_authorization_request(request_handle)
        if loaded is None:
            return self._invalid_consent_response()
        auth_request, client = loaded
        return self._render_consent(auth_request, client, request_handle=request_handle)

    async def submit_consent(self, request: Request) -> Response:
        form = await request.form()
        request_handle = form.get("request")
        action = form.get("action")
        if not isinstance(request_handle, str) or not isinstance(action, str):
            return self._invalid_consent_response()
        loaded = await self._load_authorization_request(request_handle)
        if loaded is None:
            return self._invalid_consent_response()
        auth_request, client = loaded

        if action == "deny":
            async with self.session_factory() as session:
                locked = await self._lock_authorization_request(session, request_handle)
                if locked is None:
                    return self._invalid_consent_response()
                locked.consumed_at = _now()
                await session.commit()
            return RedirectResponse(
                construct_redirect_uri(
                    auth_request.redirect_uri,
                    error="access_denied",
                    error_description="The user denied the OAuth request",
                    state=auth_request.state,
                    iss=self.issuer,
                ),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        if action != "approve":
            return self._render_consent(
                auth_request,
                client,
                request_handle=request_handle,
                error="Choose allow or deny.",
            )

        email = form.get("email")
        password = form.get("password")
        if (
            not isinstance(email, str)
            or not isinstance(password, str)
            or len(email) > 320
            or len(password) > 128
        ):
            return self._render_consent(
                auth_request,
                client,
                request_handle=request_handle,
                error="Invalid email or password.",
                status_code=401,
            )

        now = _now()
        async with self.session_factory() as session:
            user = await authenticate_password_user(
                session,
                email=email,
                password=password,
            )
            if user is None:
                return self._render_consent(
                    auth_request,
                    client,
                    request_handle=request_handle,
                    error="Invalid email or password.",
                    status_code=401,
                )
            locked = await self._lock_authorization_request(session, request_handle)
            if locked is None:
                return self._invalid_consent_response()

            authorization_id = _new_credential("hah_oauth_grant_")
            try:
                delegation = await grant_oauth_delegation(
                    session,
                    user_id=user.id,
                    issuer=self.issuer,
                    subject=f"hah-user:{user.id}",
                    oauth_client_id=locked.client_id,
                    authorization_id=authorization_id,
                    scopes=frozenset(locked.scopes),
                    commit=False,
                )
            except (OAuthDelegationValidationError, OAuthIdentityConflictError):
                return self._render_consent(
                    auth_request,
                    client,
                    request_handle=request_handle,
                    error="This account cannot authorize the requested creator tools.",
                    status_code=403,
                )

            raw_code = _new_credential("hah_oauth_code_")
            session.add(
                OAuthAuthorizationCode(
                    code_hash=_credential_hash(raw_code),
                    delegation_id=delegation.id,
                    client_id=locked.client_id,
                    authorization_id=authorization_id,
                    scopes=locked.scopes,
                    code_challenge=locked.code_challenge,
                    redirect_uri=locked.redirect_uri,
                    redirect_uri_provided_explicitly=(locked.redirect_uri_provided_explicitly),
                    resource=locked.resource,
                    expires_at=now + AUTHORIZATION_CODE_TTL,
                )
            )
            locked.consumed_at = now
            await session.commit()

        return RedirectResponse(
            construct_redirect_uri(
                auth_request.redirect_uri,
                code=raw_code,
                state=auth_request.state,
                iss=self.issuer,
            ),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def introspection(self, request: Request) -> Response:
        if not self._valid_introspection_client(request):
            return JSONResponse(
                {"error": "invalid_client"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="oauth-introspection"',
                    "Cache-Control": "no-store",
                },
            )
        form = await request.form()
        token = form.get("token")
        if not isinstance(token, str) or len(token) > 256:
            return JSONResponse({"active": False}, headers={"Cache-Control": "no-store"})
        access = await self.load_access_token(token)
        if access is None:
            return JSONResponse({"active": False}, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            {
                "active": True,
                "client_id": access.client_id,
                "scope": " ".join(access.scopes),
                "token_type": "Bearer",
                "exp": access.expires_at,
                "iat": access.issued_at,
                "sub": access.subject,
                "aud": access.resource,
                "resource": access.resource,
                "iss": self.issuer,
                "authorization_id": access.authorization_id,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _load_token(
        self,
        raw_token: str,
        *,
        token_kind: str,
        client_id: str | None = None,
    ) -> tuple[OAuthIssuedToken, OAuthIdentity] | None:
        prefix = "hah_oauth_at_" if token_kind == "access" else "hah_oauth_rt_"
        if not raw_token.startswith(prefix) or len(raw_token) > 256:
            return None
        now = _now()
        predicates = [
            OAuthIssuedToken.token_hash == _credential_hash(raw_token),
            OAuthIssuedToken.token_kind == token_kind,
            OAuthIssuedToken.revoked_at.is_(None),
            OAuthIssuedToken.expires_at > now,
            OAuthDelegation.status == IntegrationStatus.ACTIVE,
            OAuthDelegation.revoked_at.is_(None),
            OAuthDelegation.authorization_id == OAuthIssuedToken.authorization_id,
            OAuthIdentity.status == IntegrationStatus.ACTIVE,
            User.can_create_tasks.is_(True),
        ]
        if client_id is not None:
            predicates.append(OAuthIssuedToken.client_id == client_id)
        async with self.session_factory() as session:
            result = await session.execute(
                select(OAuthIssuedToken, OAuthIdentity)
                .join(OAuthDelegation, OAuthDelegation.id == OAuthIssuedToken.delegation_id)
                .join(OAuthIdentity, OAuthIdentity.id == OAuthDelegation.identity_id)
                .join(User, User.id == OAuthIdentity.user_id)
                .where(*predicates)
            )
            return result.one_or_none()

    async def _active_delegation(
        self,
        session: AsyncSession,
        delegation_id: UUID,
        authorization_id: str,
        client_id: str,
    ) -> OAuthDelegation | None:
        return await session.scalar(
            select(OAuthDelegation).where(
                OAuthDelegation.id == delegation_id,
                OAuthDelegation.oauth_client_id == client_id,
                OAuthDelegation.authorization_id == authorization_id,
                OAuthDelegation.status == IntegrationStatus.ACTIVE,
                OAuthDelegation.revoked_at.is_(None),
            )
        )

    def _issue_token_pair(
        self,
        session: AsyncSession,
        *,
        delegation_id: UUID,
        client_id: str,
        authorization_id: str,
        scopes: list[str],
        resource: str,
        family_id: UUID,
        now: datetime,
    ) -> OAuthToken:
        access_token = _new_credential("hah_oauth_at_")
        refresh_token = _new_credential("hah_oauth_rt_")
        for raw_token, token_kind, ttl in (
            (access_token, "access", ACCESS_TOKEN_TTL),
            (refresh_token, "refresh", REFRESH_TOKEN_TTL),
        ):
            session.add(
                OAuthIssuedToken(
                    token_hash=_credential_hash(raw_token),
                    token_kind=token_kind,
                    delegation_id=delegation_id,
                    client_id=client_id,
                    authorization_id=authorization_id,
                    scopes=sorted(set(scopes)),
                    resource=resource,
                    family_id=family_id,
                    expires_at=now + ttl,
                    created_at=now,
                )
            )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
            scope=" ".join(sorted(set(scopes))),
            refresh_token=refresh_token,
        )

    async def _load_authorization_request(
        self,
        request_handle: str,
    ) -> tuple[OAuthAuthorizationRequest, OAuthClientInformationFull] | None:
        if not request_handle.startswith("hah_oauth_request_") or len(request_handle) > 256:
            return None
        async with self.session_factory() as session:
            record = await session.scalar(
                select(OAuthAuthorizationRequest).where(
                    OAuthAuthorizationRequest.request_hash == _credential_hash(request_handle),
                    OAuthAuthorizationRequest.consumed_at.is_(None),
                    OAuthAuthorizationRequest.expires_at > _now(),
                )
            )
        if record is None:
            return None
        client = await self.get_client(record.client_id)
        if client is None:
            return None
        return record, client

    async def _lock_authorization_request(
        self,
        session: AsyncSession,
        request_handle: str,
    ) -> OAuthAuthorizationRequest | None:
        return await session.scalar(
            select(OAuthAuthorizationRequest)
            .where(
                OAuthAuthorizationRequest.request_hash == _credential_hash(request_handle),
                OAuthAuthorizationRequest.consumed_at.is_(None),
                OAuthAuthorizationRequest.expires_at > _now(),
            )
            .with_for_update()
        )

    def _valid_introspection_client(self, request: Request) -> bool:
        expected_id = (self.settings.mcp_oauth_introspection_client_id or "").strip()
        expected_secret = (
            self.settings.mcp_oauth_introspection_client_secret.get_secret_value()
            if self.settings.mcp_oauth_introspection_client_secret is not None
            else ""
        )
        supplied_id = ""
        supplied_secret = ""
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                supplied_id, supplied_secret = map(unquote, decoded.split(":", 1))
            except (binascii.Error, UnicodeError, ValueError):
                pass
        return (
            bool(expected_id and expected_secret)
            and hmac.compare_digest(supplied_id.encode("utf-8"), expected_id.encode("utf-8"))
            and hmac.compare_digest(
                supplied_secret.encode("utf-8"), expected_secret.encode("utf-8")
            )
        )

    def _render_consent(
        self,
        auth_request: OAuthAuthorizationRequest,
        client: OAuthClientInformationFull,
        *,
        request_handle: str,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        client_name = html.escape(client.client_name or "an MCP client")
        script_nonce = secrets.token_urlsafe(18)
        scope_items = "".join(
            '<li><span class="check">&#10003;</span><div>'
            f"<strong>{html.escape(_SCOPE_DESCRIPTIONS.get(scope, (scope, ''))[0])}</strong>"
            f"<small>{html.escape(_SCOPE_DESCRIPTIONS.get(scope, ('', scope))[1])}</small>"
            f"<code>{html.escape(scope)}</code></div></li>"
            for scope in auth_request.scopes
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        escaped_handle = html.escape(request_handle)
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Authorize MCP access</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}
body{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#eaf7f4;margin:0;
padding:2rem 1rem;color:#142d29;line-height:1.45}}
main{{max-width:34rem;margin:auto;background:white;border:1px solid #d5e6e1;border-radius:1rem;
padding:2rem;box-shadow:0 18px 50px #1232}}
.eyebrow{{color:#27655a;font-size:.76rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase}}
h1{{font-size:1.8rem;line-height:1.15;margin:.45rem 0 .6rem}}p{{margin:.5rem 0;color:#52645f}}
ul{{list-style:none;margin:1.4rem 0;padding:0;display:grid;gap:.65rem}}
li{{display:grid;grid-template-columns:auto 1fr;gap:.7rem;padding:.8rem;border:1px solid #dce9e5;
border-radius:.7rem;background:#fbfdfc}}li div{{display:grid;gap:.12rem}}
.check{{display:grid;place-items:center;width:1.45rem;height:1.45rem;border-radius:50%;
background:#dff5ed;color:#176c50;font-weight:800}}small{{color:#52645f}}code{{color:#6b7773;font-size:.72rem}}
.account{{margin-top:1.5rem;padding-top:1.4rem;border-top:1px solid #dce9e5}}
label{{display:block;margin-top:1rem;font-weight:700;font-size:.9rem}}
input{{width:100%;padding:.78rem;margin-top:.38rem;border:1px solid #aebfba;border-radius:.55rem;
font:inherit}}input:focus{{outline:3px solid #a9dfd1;border-color:#27655a}}
.actions{{display:flex;align-items:center;gap:.55rem;flex-wrap:wrap;margin-top:1.25rem}}
button{{padding:.78rem 1rem;border:0;border-radius:.55rem;cursor:pointer;
font:inherit;font-weight:750}}
button:disabled{{cursor:wait;opacity:.72}}
.allow{{background:#123f37;color:white}}.deny{{background:#edf1f0;color:#263b36}}
.error{{padding:.7rem;border-radius:.5rem;background:#fff0f0;color:#a11717}}
.device{{padding:.75rem;border-radius:.6rem;background:#f0f8f6;color:#294d45;font-size:.88rem}}
.security{{display:flex;gap:.45rem;align-items:flex-start;margin-top:1rem;font-size:.8rem}}
</style></head><body><main><span class="eyebrow">HAH secure connection</span>
<h1>Connect {client_name}</h1>
<p>Review what this MCP client is requesting. You stay in control and can deny access.</p>
<p class="device"><strong>Connecting another computer?</strong> Use the same HAH account,
but finish this page on the computer where your AI app started the connection.</p>
<ul>{scope_items}</ul>{error_html}
<form id="oauth-consent-form" method="post" action="/oauth/consent">
<input type="hidden" name="request" value="{escaped_handle}">
<div class="account"><strong>Sign in to approve</strong>
<p>Use the same creator account as your HAH dashboard.</p></div>
<label>Email<input name="email" type="email" autocomplete="username" required
maxlength="320"></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required
maxlength="128"></label>
<div class="actions"><button class="allow" name="action" value="approve" type="submit">
Allow MCP access once</button>
<button class="deny" name="action" value="deny" type="submit" formnovalidate>
Deny</button></div></form>
<p class="security"><span>&#128274;</span><small>Your password is verified only by HAH
and is never shared with the MCP client.</small></p>
<script nonce="{script_nonce}">
const form = document.getElementById('oauth-consent-form');
form.addEventListener('submit', (event) => {{
  const submitter = event.submitter;
  window.setTimeout(() => {{
    for (const button of form.querySelectorAll('button')) button.disabled = true;
    if (submitter && submitter.value === 'approve') submitter.textContent = 'Connecting…';
  }}, 0);
}});
</script>
</main></body></html>"""
        content_security_policy = (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{script_nonce}'; "
            "form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return HTMLResponse(
            body,
            status_code=status_code,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": content_security_policy,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _invalid_consent_response(self) -> HTMLResponse:
        body = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Start a new HAH connection</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{font-family:Inter,ui-sans-serif,system-ui,
sans-serif;background:#eaf7f4;margin:0;padding:2rem 1rem;color:#142d29;line-height:1.5}
main{max-width:32rem;margin:8vh auto;background:white;border:1px solid #d5e6e1;border-radius:1rem;
padding:2rem;box-shadow:0 18px 50px #1232}.eyebrow{color:#27655a;font-size:.76rem;font-weight:800;
letter-spacing:.08em;text-transform:uppercase}h1{font-size:1.8rem;line-height:1.15;margin:.5rem 0}
p{color:#52645f}ol{padding-left:1.25rem}li{margin:.6rem 0}strong{color:#142d29}
</style></head><body><main><span class="eyebrow">HAH secure connection</span>
<h1>Start a new connection</h1>
<p>This one-time authorization page has expired or was already submitted.</p>
<ol><li>Return to the AI app <strong>on this computer</strong>.</li>
<li>Choose <strong>Authenticate</strong> for HAH again.</li>
<li>Keep the app open and use only the new browser page.</li></ol>
<p>You can use the same HAH account on every computer. Each computer must start its own secure
connection.</p></main></body></html>"""
        return HTMLResponse(
            body,
            status_code=400,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                    "frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
