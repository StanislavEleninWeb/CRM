"""Validated application settings loaded from the environment."""

from datetime import date
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_PREFIX = "se"


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

    # Provider-secret encryption. Comma-separated "version:base64-32-byte-key", newest first.
    # The first entry encrypts new secrets; every entry can decrypt.
    secret_encryption_keys: str = ""

    # Gmail for the internal pilot only. An "Internal" Google OAuth app serves one Workspace
    # organisation; only the tenants listed here may connect a mailbox, and only from that domain.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_internal_domain: str = ""
    gmail_internal_tenant_ids: str = ""
    gmail_pubsub_topic: str = ""
    gmail_push_audience: str = ""
    gmail_push_service_account: str = ""
    gmail_sync_query: str = "newer_than:30d"
    mailbox_reconcile_minutes: int = Field(default=15, ge=1, le=1440)
    # External tenants cannot connect Gmail until the verification and assessment gate passes (CRM-114).
    external_gmail_enabled: bool = False
    # Evidence for the external Gmail gate (CRM-114). The flag alone opens nothing.
    external_gmail_client_id: str = ""
    external_gmail_verification_ref: str = ""
    external_gmail_assessment_valid_until: date | None = None

    # Keys the hashes that remember erased businesses. Changing it un-erases them: every
    # tombstone would have to be recreated. Falls back to the session secret when empty.
    erasure_hash_key: str = ""

    # Billing. "off": every workspace has the internal pilot's allowances and nothing is charged.
    # "test": plans, trials and limits apply, against Stripe test mode. "live" is refused until
    # approved plans and a live account exist.
    billing_mode: Literal["off", "test"] = "off"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    trial_days: int = Field(default=14, ge=1, le=90)
    past_due_grace_days: int = Field(default=7, ge=0, le=60)

    # Whether approved messages leave the building. "off": send requests are refused.
    # "dry_run": every step runs except the provider call. "live": messages are sent.
    email_dispatch: Literal["off", "dry_run", "live"] = "off"
    send_lease_seconds: int = Field(default=90, ge=10, le=900)
    send_schedule_max_days: int = Field(default=60, ge=1, le=365)

    s3_endpoint_url: str = ""
    s3_bucket: str = "crm-local"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

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
        if self.stripe_secret_key.startswith(("sk_live", "rk_live")):
            raise ValueError("a live Stripe key is not accepted: live billing has not been approved")
        if self.environment in ("staging", "production"):
            for name in (
                "session_secret",
                "database_url",
                "oidc_client_secret",
                "s3_secret_key",
                "gmail_client_secret",
                "stripe_secret_key",
                "stripe_webhook_secret",
            ):
                if PLACEHOLDER_PREFIX in str(getattr(self, name)):
                    raise ValueError(f"{name} still contains a placeholder value")
            if self.oidc_dev_provider:
                raise ValueError("the development identity provider is not allowed here")
            for name in ("oidc_issuer", "public_base_url"):
                if not str(getattr(self, name)).startswith("https://"):
                    raise ValueError(f"{name} must use https")
        return self

    @property
    def gmail_internal_tenants(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.gmail_internal_tenant_ids.split(",") if part.strip())

    @property
    def cookies_secure(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def is_production_like(self) -> bool:
        return self.environment in ("staging", "production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
