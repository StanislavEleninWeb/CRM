"""Validated application settings loaded from the environment."""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_PREFIX = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"

    database_url: str
    migration_database_url: str | None = None
    redis_url: str = "redis://redis:6379/0"

    session_secret: str = Field(min_length=32)
    public_base_url: str = "http://localhost:5173"
    api_base_url: str = "http://localhost:8000"

    # OIDC. ``oidc_issuer`` is the exact ``iss`` value and the browser-facing address.
    # ``oidc_internal_url`` is how the API reaches the same provider (back channel).
    oidc_issuer: str = ""
    oidc_internal_url: str | None = None
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_dev_provider: bool = False
    session_ttl_hours: int = Field(default=12, ge=1, le=24 * 30)
    invitation_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)

    default_currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    default_timezone: str = "Europe/Sofia"

    due_poll_interval_seconds: int = Field(default=30, ge=5, le=3600)
    due_poll_batch_size: int = Field(default=50, ge=1, le=1000)

    @field_validator("default_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _postgres_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("postgresql+psycopg://"):
            raise ValueError("only postgresql+psycopg:// URLs are supported")
        return value

    @model_validator(mode="after")
    def _no_placeholders_outside_development(self) -> "Settings":
        if self.environment in ("staging", "production"):
            for name in ("session_secret", "database_url", "oidc_client_secret"):
                if PLACEHOLDER_PREFIX in str(getattr(self, name)):
                    raise ValueError(f"{name} still contains a placeholder value")
            if self.oidc_dev_provider:
                raise ValueError("the development identity provider is not allowed here")
            for name in ("oidc_issuer", "public_base_url"):
                if not str(getattr(self, name)).startswith("https://"):
                    raise ValueError(f"{name} must use https")
        return self

    @property
    def cookies_secure(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def is_production_like(self) -> bool:
        return self.environment in ("staging", "production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
