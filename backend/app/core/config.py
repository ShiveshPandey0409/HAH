from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db.database_url import normalize_async_database_url

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
    webhook_worker_enabled: bool = False
    http_session_ttl_seconds: int = 604800
    password_reset_ttl_seconds: int = 900
    password_reset_url: AnyHttpUrl = AnyHttpUrl("http://localhost:3000/reset-password")
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_email: EmailStr | None = None

    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        return normalize_async_database_url(value)

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
        if not 300 <= self.http_session_ttl_seconds <= 2_678_400:
            raise ValueError("HTTP session TTL must be between 5 minutes and 31 days")
        if not 300 <= self.password_reset_ttl_seconds <= 3600:
            raise ValueError("password reset TTL must be between 5 and 60 minutes")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535")
        smtp_host = (self.smtp_host or "").strip()
        smtp_username = (self.smtp_username or "").strip()
        smtp_password = (
            self.smtp_password.get_secret_value() if self.smtp_password is not None else ""
        )
        if bool(smtp_host) != (self.smtp_from_email is not None):
            raise ValueError("SMTP host and from email must be configured together")
        if bool(smtp_username) != bool(smtp_password):
            raise ValueError("SMTP username and password must be configured together")
        if smtp_username and not smtp_host:
            raise ValueError("SMTP authentication requires an SMTP host")
        if self.app_env in {"staging", "production"}:
            configured_keys = {
                key.get_secret_value() for key in self.webhook_secret_encryption_keys
            }
            if not configured_keys:
                raise ValueError(f"{self.app_env} requires a webhook encryption key")
            if DEVELOPMENT_WEBHOOK_ENCRYPTION_KEY in configured_keys:
                raise ValueError("the development webhook encryption key is not deployment-safe")
        self._validate_mcp_oauth_settings()
        if (
            self.app_env in {"staging", "production"}
            and self.smtp_configured
            and self.password_reset_url.scheme != "https"
        ):
            raise ValueError("deployed password reset URLs must use HTTPS")
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

        if self.app_env in {"staging", "production"} and all(configured):
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

    @property
    def smtp_configured(self) -> bool:
        return bool((self.smtp_host or "").strip()) and self.smtp_from_email is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
