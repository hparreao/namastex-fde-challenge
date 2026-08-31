from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://autoseguro:autoseguro@localhost:5432/autoseguro"
    quote_service_url: str = "http://localhost:8000"
    quote_timeout_seconds: float = 3.0
    quote_max_attempts: int = 3
    quote_backoff_seconds: float = 0.25
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.4-mini"
    openai_api_key: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)
    auto_create_schema: bool = False
    log_level: str = "INFO"
    cors_allowed_origins: str = ""
    enable_hsts: bool = False
    rate_limit_enabled: bool = True
    global_rate_limit: int = Field(default=120, ge=1)
    session_create_rate_limit: int = Field(default=20, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    max_messages_per_session: int = Field(default=100, ge=10, le=500)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: float = Field(default=3.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    coordination_backend: str = "local"
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "autoseguro"
    idempotency_ttl_seconds: int = Field(default=86400, ge=60)
    session_lock_ttl_seconds: int = Field(default=30, ge=10)
    session_lock_wait_seconds: float = Field(default=12.0, ge=0, le=30)
    circuit_breaker_threshold: int = Field(default=3, ge=1)
    circuit_breaker_open_seconds: int = Field(default=30, ge=1)
    telemetry_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318/v1/traces"
    otel_exporter_otlp_headers: str = Field(default="", repr=False)
    telemetry_service_name: str = "autoseguro-agent"
    policy_mode: str = "shadow"
    cedar_policy_path: str = "policies/autoseguro.cedar"
    cedar_schema_path: str = "policies/autoseguro.cedarschema"
    policy_enforce_actions: str = "CallQuote"

    @model_validator(mode="after")
    def validate_session_lock_budget(self) -> Self:
        retry_backoff_budget = self.quote_backoff_seconds * (
            2 ** max(0, self.quote_max_attempts - 1) - 1
        )
        minimum = self.quote_max_attempts * self.quote_timeout_seconds + retry_backoff_budget + 2
        if self.session_lock_ttl_seconds < minimum:
            raise ValueError(
                "SESSION_LOCK_TTL_SECONDS deve cobrir timeout, retries, backoff e margem"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
