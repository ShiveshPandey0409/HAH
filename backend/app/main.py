from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from starlette.routing import Route

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import AsyncSessionFactory, engine
from app.mcp.server import MCP_SUPPORTED_SCOPES, create_mcp_server
from app.services.enrichment import HackathonSelfAttestedEnrichmentProvider
from app.services.oauth_authorization_server import FirstPartyOAuthProvider
from app.services.payments import (
    PaymentProviderUnavailableError,
)
from app.services.payments import (
    runtime_from_settings as payment_runtime_from_settings,
)
from app.workers.payments import run_payment_worker
from app.workers.webhooks import run_webhook_worker, runtime_from_settings

settings = get_settings()


async def safe_request_validation_error(
    _: Request,
    error: RequestValidationError,
) -> JSONResponse:
    safe_errors = [
        {key: value for key, value in item.items() if key not in {"input", "ctx"}}
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": safe_errors},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    stop_event = asyncio.Event()
    worker_tasks: list[asyncio.Task[None]] = []
    if settings.webhook_worker_enabled and app.state.webhook_runtime is not None:
        worker_tasks.append(
            asyncio.create_task(
                run_webhook_worker(
                    runtime=app.state.webhook_runtime,
                    stop_event=stop_event,
                )
            )
        )
    if settings.prava_payment_worker_enabled and app.state.payment_runtime is not None:
        worker_tasks.append(
            asyncio.create_task(
                run_payment_worker(
                    runtime=app.state.payment_runtime,
                    stop_event=stop_event,
                )
            )
        )
    try:
        async with app.state.mcp_server.session_manager.run():
            yield
    finally:
        if worker_tasks:
            stop_event.set()
            await asyncio.gather(*worker_tasks)
        await engine.dispose()


def create_app() -> FastAPI:
    mcp_server, mcp_http_app = create_mcp_server()
    oauth_provider = FirstPartyOAuthProvider(
        settings=settings,
        session_factory=AsyncSessionFactory,
    )
    application = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.web_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/", include_in_schema=False)
    async def root_documentation() -> RedirectResponse:
        return RedirectResponse(url="/docs", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @application.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    async def oauth_authorization_server_metadata() -> JSONResponse:
        issuer = str(settings.mcp_oauth_issuer_url)
        endpoint_base = issuer.rstrip("/")
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{endpoint_base}/authorize",
                "token_endpoint": f"{endpoint_base}/token",
                "registration_endpoint": f"{endpoint_base}/register",
                "revocation_endpoint": f"{endpoint_base}/revoke",
                "introspection_endpoint": f"{endpoint_base}/oauth/introspect",
                "scopes_supported": list(MCP_SUPPORTED_SCOPES),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "revocation_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
            },
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    application.state.mcp_server = mcp_server
    application.state.oauth_provider = oauth_provider
    if settings.hackathon_social_self_attestation_enabled:
        application.state.enrichment_provider = HackathonSelfAttestedEnrichmentProvider()
    try:
        application.state.webhook_runtime = runtime_from_settings()
    except RuntimeError:
        if settings.app_env in {"staging", "production"}:
            raise
        application.state.webhook_runtime = None
    try:
        application.state.payment_runtime = payment_runtime_from_settings(settings)
    except PaymentProviderUnavailableError:
        application.state.payment_runtime = None
    application.add_exception_handler(RequestValidationError, safe_request_validation_error)
    application.include_router(api_router)
    application.router.routes.extend(
        create_auth_routes(
            oauth_provider,
            issuer_url=settings.mcp_oauth_issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=list(MCP_SUPPORTED_SCOPES),
                default_scopes=list(MCP_SUPPORTED_SCOPES),
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    )
    application.router.routes.extend(
        [
            Route("/oauth/consent", oauth_provider.consent_page, methods=["GET"]),
            Route("/oauth/consent", oauth_provider.submit_consent, methods=["POST"]),
            Route("/oauth/introspect", oauth_provider.introspection, methods=["POST"]),
        ]
    )
    application.mount("/", mcp_http_app)
    return application


app = create_app()
