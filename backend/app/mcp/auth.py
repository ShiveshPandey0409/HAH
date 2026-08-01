from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.api_clients import (
    APIClientPrincipal,
    InvalidAPIKeyError,
    authenticate_api_token,
)

_current_api_client: ContextVar[APIClientPrincipal | None] = ContextVar(
    "current_api_client",
    default=None,
)


class MissingAPIClientContextError(Exception):
    pass


def get_current_api_client() -> APIClientPrincipal:
    principal = _current_api_client.get()
    if principal is None:
        raise MissingAPIClientContextError("authenticated API client context is required")
    return principal


@contextmanager
def use_api_client(principal: APIClientPrincipal) -> Iterator[None]:
    token = _current_api_client.set(principal)
    try:
        yield
    finally:
        _current_api_client.reset(token)


class APIKeyAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # This middleware wraps the MCP sub-application, which is mounted last as
        # FastAPI's fallback. Only the actual MCP endpoint should trigger API-key
        # authentication; unrelated unknown paths must retain normal 404 behavior.
        if scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, separator, credentials = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not credentials.strip():
            await self._unauthorized(scope, receive, send)
            return

        try:
            principal = await authenticate_api_token(credentials.strip())
        except InvalidAPIKeyError:
            await self._unauthorized(scope, receive, send)
            return

        context_token = _current_api_client.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_api_client.reset(context_token)

    @staticmethod
    async def _unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"error": "unauthorized", "message": "A valid bearer API key is required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
