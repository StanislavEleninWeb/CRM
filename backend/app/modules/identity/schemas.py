from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from app.core.normalize import normalize_email
from app.modules.identity.permissions import Role


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str


class TenantSummary(BaseModel):
    id: UUID
    name: str
    role: Role


class ActiveTenant(BaseModel):
    id: UUID
    name: str
    role: Role
    permissions: list[str]
    currency: str
    timezone: str


class Me(BaseModel):
    user: UserOut
    active_tenant: ActiveTenant | None
    tenants: list[TenantSummary]
    csrf_token: str
    mfa_claimed: bool


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except Exception as exc:
                raise ValueError("unknown time zone") from exc
        return value


class TenantOut(BaseModel):
    id: UUID
    name: str
    currency: str
    timezone: str


class SwitchTenant(BaseModel):
    tenant_id: UUID


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    role: Role
    joined_at: datetime


class MemberUpdate(BaseModel):
    role: Role


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Role = Role.REPRESENTATIVE

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        if normalized is None:
            raise ValueError("enter a valid email address")
        return normalized


InvitationStatus = Literal["pending", "accepted", "expired", "revoked"]


class InvitationOut(BaseModel):
    id: UUID
    email: str
    role: Role
    status: InvitationStatus
    expires_at: datetime
    created_at: datetime


class InvitationCreated(InvitationOut):
    token: str = Field(description="Shown once. Only its hash is stored.")
    accept_url: str


class InvitationPeek(BaseModel):
    tenant_name: str
    email: str
    role: Role
    status: InvitationStatus


class InvitationAccept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class SessionOut(BaseModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str
    current: bool


class AuditEventOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor_type: str
    actor_id: UUID | None
    action: str
    target_type: str
    target_id: str | None
    origin: str
    correlation_id: str | None
    data: dict[str, object]
