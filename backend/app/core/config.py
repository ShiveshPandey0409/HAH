from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEVELOPMENT_WEBHOOK_ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
AppEnvironment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    app_name: str = "Hire a Human API"
    app_env: AppEnvironment = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/hire_human"
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
