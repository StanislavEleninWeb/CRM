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
            for name in ("session_secret", "database_url"):
                if PLACEHOLDER_PREFIX in str(getattr(self, name)):
                    raise ValueError(f"{name} still contains a placeholder value")
        return self

    @property
    def is_production_like(self) -> bool:
        return self.environment in ("staging", "production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
