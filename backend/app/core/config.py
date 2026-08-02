from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEVELOPMENT_WEBHOOK_ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
AppEnvironment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    app_name: str = "Hire a Human API"
    app_env: AppEnvironment = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/hire_human"
    mcp_public_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000/mcp")
    mcp_oauth_issuer_url: AnyHttpUrl = AnyHttpUrl("http://localhost:9000")
    mcp_oauth_introspection_url: AnyHttpUrl | None = None
    mcp_oauth_introspection_client_id: str | None = None
    mcp_oauth_introspection_client_secret: SecretStr | None = None
    mcp_oauth_introspection_timeout_seconds: float = 5.0
    mcp_oauth_clock_skew_seconds: int = 30
    mcp_oauth_max_token_lifetime_seconds: int = 3600
    mcp_allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    mcp_allowed_origins: list[str] = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    webhook_secret_encryption_keys: list[SecretStr] = Field(default_factory=list)
    webhook_max_attempts: int = 6
    webhook_timeout_seconds: float = 10.0
    webhook_dns_timeout_seconds: float = 3.0
    webhook_lease_seconds: int = 30
    webhook_retry_base_seconds: int = 30
    webhook_retry_cap_seconds: int = 3600
    webhook_poll_interval_seconds: float = 1.0
    webhook_response_body_limit: int = 4096

    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    @model_validator(mode="after")
    def validate_webhook_worker_settings(self) -> Settings:
        positive_values = (
            self.webhook_max_attempts,
            self.webhook_timeout_seconds,
            self.webhook_dns_timeout_seconds,
            self.webhook_lease_seconds,
            self.webhook_retry_base_seconds,
            self.webhook_retry_cap_seconds,
            self.webhook_poll_interval_seconds,
            self.webhook_response_body_limit,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("webhook worker settings must be positive")
        if self.webhook_lease_seconds <= (
            self.webhook_dns_timeout_seconds + self.webhook_timeout_seconds
        ):
            raise ValueError("webhook lease must exceed DNS and delivery timeouts")
        if self.webhook_retry_cap_seconds < self.webhook_retry_base_seconds:
            raise ValueError("webhook retry cap must not be lower than its base")
        if self.app_env in {"staging", "production"}:
            configured_keys = {
                key.get_secret_value() for key in self.webhook_secret_encryption_keys
            }
            if not configured_keys:
                raise ValueError(f"{self.app_env} requires a webhook encryption key")
            if DEVELOPMENT_WEBHOOK_ENCRYPTION_KEY in configured_keys:
                raise ValueError("the development webhook encryption key is not deployment-safe")
        self._validate_mcp_oauth_settings()
        return self

    def _validate_mcp_oauth_settings(self) -> None:
        if self.mcp_public_url.path != "/mcp":
            raise ValueError("MCP_PUBLIC_URL must be the canonical public /mcp endpoint")
        for name, url in (
            ("MCP_PUBLIC_URL", self.mcp_public_url),
            ("MCP_OAUTH_ISSUER_URL", self.mcp_oauth_issuer_url),
            ("MCP_OAUTH_INTROSPECTION_URL", self.mcp_oauth_introspection_url),
        ):
            if url is not None and (url.username is not None or url.password is not None):
                raise ValueError(f"{name} cannot contain user information")
            if url is not None and (url.query is not None or url.fragment is not None):
                raise ValueError(f"{name} cannot contain a query string or fragment")
            if (
                url is not None
                and url.scheme != "https"
                and url.host not in {"localhost", "127.0.0.1", "[::1]", "::1"}
            ):
                raise ValueError(f"{name} must use HTTPS except on a loopback host")

        client_id = (self.mcp_oauth_introspection_client_id or "").strip()
        client_secret = (
            self.mcp_oauth_introspection_client_secret.get_secret_value()
            if self.mcp_oauth_introspection_client_secret is not None
            else ""
        )
        configured = (
            self.mcp_oauth_introspection_url is not None,
            bool(client_id),
            bool(client_secret),
        )
        if any(configured) and not all(configured):
            raise ValueError(
                "OAuth introspection URL, client ID, and client secret must be configured together"
            )
        if self.mcp_oauth_introspection_timeout_seconds <= 0:
            raise ValueError("MCP OAuth introspection timeout must be positive")
        if not 0 <= self.mcp_oauth_clock_skew_seconds <= 300:
            raise ValueError("MCP OAuth clock skew must be between 0 and 300 seconds")
        if self.mcp_oauth_max_token_lifetime_seconds <= 0:
            raise ValueError("MCP OAuth maximum token lifetime must be positive")

        if self.app_env in {"staging", "production"}:
            if not all(configured):
                raise ValueError(f"{self.app_env} requires OAuth token introspection credentials")
            deployment_urls = (
                self.mcp_public_url,
                self.mcp_oauth_issuer_url,
                self.mcp_oauth_introspection_url,
            )
            if any(url is not None and url.scheme != "https" for url in deployment_urls):
                raise ValueError("deployed MCP OAuth endpoints must use HTTPS")

    @property
    def mcp_oauth_introspection_configured(self) -> bool:
        return self.mcp_oauth_introspection_url is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
