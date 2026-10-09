"""API keys and webhook endpoints. Managed by signed-in administrators; never by an API key."""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core import apikeys, outbox
from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError
from app.core.pagination import Page, PageParams, page_params
from app.core.security import hash_token
from app.core.time import utcnow
from app.modules.crm.common import ValidationFailed, execute, many, one, scalar
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.modules.integrations import webhooks
from app.worker.due import schedule

router = APIRouter()
MANAGE = tenant_with(Permission.INTEGRATIONS_MANAGE)
Paging = Annotated[PageParams, Depends(page_params)]


# --- API keys --------------------------------------------------------------------------------


class ApiKeyOut(BaseModel):
    id: UUID
    name: str
    prefix: str = Field(description="The first characters of the key, to tell keys apart. Not enough to use it.")
    scopes: list[str]
    rate_limit_per_minute: int
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    usable: bool


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Shown this once. It cannot be retrieved later.")


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=20)
    expires_in_days: int | None = Field(
        default=90, ge=1, le=730, description="Leave empty for a key that does not expire"
    )
    rate_limit_per_minute: int = Field(default=120, ge=1, le=6000)


class ScopeOut(BaseModel):
    name: str
    grantable: bool = Field(description="False when your own role does not include it")


KEY_COLUMNS = (
    "id, name, prefix, scopes, rate_limit_per_minute, created_at, expires_at, revoked_at, last_used_at, created_by"
)


def _key(row: Any) -> dict[str, Any]:
    live = row["revoked_at"] is None and row["created_by"] is not None
    return {**dict(row), "usable": live and (row["expires_at"] is None or row["expires_at"] > utcnow())}


@router.get("/api-keys/scopes", response_model=list[ScopeOut], operation_id="listApiKeyScopes", tags=["api keys"])
def list_scopes(ctx: TenantContext = MANAGE) -> list[ScopeOut]:
    """Everything a key can be allowed to do. Ownership, settings, billing, credentials and approvals are not on the list."""
    return [
        ScopeOut(name=p.value, grantable=p in ctx.permissions)
        for p in sorted(apikeys.ALLOWED_SCOPES, key=lambda p: p.value)
    ]


@router.get("/api-keys", response_model=list[ApiKeyOut], operation_id="listApiKeys", tags=["api keys"])
def list_keys(ctx: TenantContext = MANAGE) -> list[ApiKeyOut]:
    rows = many(ctx, f"SELECT {KEY_COLUMNS} FROM api_keys WHERE tenant_id = :tenant_id ORDER BY created_at DESC")
    return [ApiKeyOut(**_key(r)) for r in rows]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201, operation_id="createApiKey", tags=["api keys"])
def create_key(body: ApiKeyIn, ctx: TenantContext = MANAGE) -> ApiKeyCreated:
    """Create a key that acts as you, limited to the chosen scopes. The key is returned once."""
    allowed = {p.value: p for p in apikeys.ALLOWED_SCOPES}
    unknown = sorted(set(body.scopes) - set(allowed))
    if unknown:
        raise ValidationFailed(f"These cannot be given to an API key: {', '.join(unknown)}.")
    beyond = sorted(s for s in set(body.scopes) if allowed[s] not in ctx.permissions)
    if beyond:
        raise ValidationFailed(f"Your own role does not include: {', '.join(beyond)}.")
    token, prefix = apikeys.generate()
    key_id = scalar(
        ctx,
        "INSERT INTO api_keys (tenant_id, name, prefix, key_hash, scopes, rate_limit_per_minute, created_by, expires_at) "
        "VALUES (:tenant_id, :n, :p, :h, :s, :r, :u, :e) RETURNING id",
        {
            "n": body.name.strip(),
            "p": prefix,
            "h": hash_token(token),
            "s": sorted(set(body.scopes)),
            "r": body.rate_limit_per_minute,
            "u": ctx.user_id,
            "e": utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None,
        },
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="api_key.created",
        target_type="api_key",
        target_id=str(key_id),
        data={"name": body.name.strip(), "scopes": sorted(set(body.scopes)), "expires_in_days": body.expires_in_days},
    )
    row = one(
        ctx,
        f"SELECT {KEY_COLUMNS} FROM api_keys WHERE tenant_id = :tenant_id AND id = :id",
        {"id": key_id},
        "Not found.",
    )
    return ApiKeyCreated(**_key(row), key=token)


@router.delete("/api-keys/{key_id}", response_model=ApiKeyOut, operation_id="revokeApiKey", tags=["api keys"])
def revoke_key(key_id: UUID, ctx: TenantContext = MANAGE) -> ApiKeyOut:
    row = one(
        ctx,
        "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()), revoked_by = COALESCE(revoked_by, :u) "
        f"WHERE tenant_id = :tenant_id AND id = :id RETURNING {KEY_COLUMNS}",
        {"id": key_id, "u": ctx.user_id},
        "API key not found.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="api_key.revoked",
        target_type="api_key",
        target_id=str(key_id),
    )
    return ApiKeyOut(**_key(row))


# --- webhooks --------------------------------------------------------------------------------


class EndpointOut(BaseModel):
    id: UUID
    url: str
    description: str | None
    event_types: list[str]
    status: Literal["active", "paused"]
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    old_signature_until: datetime | None = Field(
        description="After a rotation: until then, deliveries also carry a signature made with the earlier secret"
    )
    created_at: datetime


class EndpointCreated(EndpointOut):
    secret: str = Field(description="The signing secret. Shown this once.")


class EndpointIn(BaseModel):
    url: str = Field(min_length=10, max_length=2000)
    description: str | None = Field(default=None, max_length=300)
    event_types: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=30)


class EndpointUpdate(BaseModel):
    status: Literal["active", "paused"] | None = None
    description: str | None = Field(default=None, max_length=300)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=30)


class DeliveryOut(BaseModel):
    id: UUID
    endpoint_id: UUID
    event_id: UUID
    event_type: str
    status: Literal["pending", "delivered", "dead"]
    attempts: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


ENDPOINT_COLUMNS = (
    "id, url, description, event_types, status, consecutive_failures, last_success_at, last_failure_at, last_error, "
    "previous_secret_expires_at AS old_signature_until, created_at"
)


def _event_types(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - {"*", *webhooks.KNOWN_EVENTS})
    if unknown:
        raise ValidationFailed(f"Unknown event types: {', '.join(unknown)}.")
    return ["*"] if "*" in values else sorted(set(values))


def _endpoint(ctx: TenantContext, endpoint_id: UUID) -> Any:
    return one(
        ctx,
        f"SELECT {ENDPOINT_COLUMNS} FROM webhook_endpoints WHERE tenant_id = :tenant_id AND id = :id",
        {"id": endpoint_id},
        "Webhook endpoint not found.",
    )


@router.get("/webhook-event-types", response_model=list[str], operation_id="listWebhookEventTypes", tags=["webhooks"])
def event_types(ctx: TenantContext = MANAGE) -> list[str]:
    return list(webhooks.KNOWN_EVENTS)


@router.get(
    "/webhook-endpoints", response_model=list[EndpointOut], operation_id="listWebhookEndpoints", tags=["webhooks"]
)
def list_endpoints(ctx: TenantContext = MANAGE) -> list[EndpointOut]:
    rows = many(
        ctx, f"SELECT {ENDPOINT_COLUMNS} FROM webhook_endpoints WHERE tenant_id = :tenant_id ORDER BY created_at"
    )
    return [EndpointOut(**r) for r in rows]


@router.post(
    "/webhook-endpoints",
    response_model=EndpointCreated,
    status_code=201,
    operation_id="createWebhookEndpoint",
    tags=["webhooks"],
)
def create_endpoint(body: EndpointIn, ctx: TenantContext = MANAGE) -> EndpointCreated:
    try:
        url = webhooks.check_destination(body.url.strip())
    except ValueError as exc:
        raise ValidationFailed(f"This address cannot be used: {exc}.") from exc
    if scalar(ctx, "SELECT count(*) FROM webhook_endpoints WHERE tenant_id = :tenant_id") >= 10:
        raise ConflictError("A workspace can have at most 10 webhook endpoints.")
    endpoint_id, secret = uuid4(), webhooks.new_secret()
    sealed = webhooks.seal_secret(ctx.tenant_id, endpoint_id, secret)
    execute(
        ctx,
        "INSERT INTO webhook_endpoints (id, tenant_id, url, description, event_types, secret_ciphertext, secret_nonce, "
        "secret_key_version, created_by) VALUES (:id, :tenant_id, :url, :d, :types, :c, :n, :v, :u)",
        {
            "id": endpoint_id,
            "url": url,
            "d": body.description,
            "types": _event_types(body.event_types),
            "c": sealed.ciphertext,
            "n": sealed.nonce,
            "v": sealed.key_version,
            "u": ctx.user_id,
        },
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="webhook.created",
        target_type="webhook_endpoint",
        target_id=str(endpoint_id),
        data={"url": url},
    )
    return EndpointCreated(**_endpoint(ctx, endpoint_id), secret=secret)


@router.patch(
    "/webhook-endpoints/{endpoint_id}",
    response_model=EndpointOut,
    operation_id="updateWebhookEndpoint",
    tags=["webhooks"],
)
def update_endpoint(endpoint_id: UUID, body: EndpointUpdate, ctx: TenantContext = MANAGE) -> EndpointOut:
    _endpoint(ctx, endpoint_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "event_types" in changes:
        changes["event_types"] = _event_types(changes["event_types"])
    if changes:
        assignments = ", ".join(f"{k} = :{k}" for k in changes)
        execute(
            ctx,
            f"UPDATE webhook_endpoints SET {assignments} WHERE tenant_id = :tenant_id AND id = :id",
            {**changes, "id": endpoint_id},
        )
        record_audit(
            ctx.db,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="webhook.updated",
            target_type="webhook_endpoint",
            target_id=str(endpoint_id),
            data={"changed": sorted(changes)},
        )
    return EndpointOut(**_endpoint(ctx, endpoint_id))


@router.post(
    "/webhook-endpoints/{endpoint_id}/rotate-secret",
    response_model=EndpointCreated,
    operation_id="rotateWebhookSecret",
    tags=["webhooks"],
)
def rotate_secret(endpoint_id: UUID, ctx: TenantContext = MANAGE) -> EndpointCreated:
    """Issue a new signing secret. For 24 hours deliveries are signed with both, so the receiver can switch over."""
    _endpoint(ctx, endpoint_id)
    secret = webhooks.new_secret()
    sealed = webhooks.seal_secret(ctx.tenant_id, endpoint_id, secret)
    execute(
        ctx,
        "UPDATE webhook_endpoints SET previous_secret_ciphertext = secret_ciphertext, previous_secret_nonce = secret_nonce, "
        "previous_secret_key_version = secret_key_version, previous_secret_expires_at = :until, secret_ciphertext = :c, "
        "secret_nonce = :n, secret_key_version = :v WHERE tenant_id = :tenant_id AND id = :id",
        {
            "until": utcnow() + webhooks.ROTATION_OVERLAP,
            "c": sealed.ciphertext,
            "n": sealed.nonce,
            "v": sealed.key_version,
            "id": endpoint_id,
        },
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="webhook.secret_rotated",
        target_type="webhook_endpoint",
        target_id=str(endpoint_id),
    )
    return EndpointCreated(**_endpoint(ctx, endpoint_id), secret=secret)


@router.delete(
    "/webhook-endpoints/{endpoint_id}", status_code=204, operation_id="deleteWebhookEndpoint", tags=["webhooks"]
)
def delete_endpoint(endpoint_id: UUID, ctx: TenantContext = MANAGE) -> None:
    _endpoint(ctx, endpoint_id)
    execute(
        ctx, "DELETE FROM webhook_deliveries WHERE tenant_id = :tenant_id AND endpoint_id = :id", {"id": endpoint_id}
    )
    execute(ctx, "DELETE FROM webhook_endpoints WHERE tenant_id = :tenant_id AND id = :id", {"id": endpoint_id})
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="webhook.deleted",
        target_type="webhook_endpoint",
        target_id=str(endpoint_id),
    )


@router.post(
    "/webhook-endpoints/{endpoint_id}/test",
    response_model=DeliveryOut,
    operation_id="testWebhookEndpoint",
    tags=["webhooks"],
)
def send_test(endpoint_id: UUID, ctx: TenantContext = MANAGE) -> DeliveryOut:
    """Queue a harmless test event for this endpoint only."""
    _endpoint(ctx, endpoint_id)
    event_id = scalar(
        ctx,
        "INSERT INTO outbox_events (tenant_id, event_type, subject_type, subject_id, payload) "
        "VALUES (:tenant_id, 'webhook.test', 'webhook_endpoint', :id, '{\"message\": \"This is a test event.\"}'::jsonb) RETURNING id",
        {"id": endpoint_id},
    )
    delivery_id = scalar(
        ctx,
        "INSERT INTO webhook_deliveries (tenant_id, endpoint_id, event_id, next_attempt_at) VALUES (:tenant_id, :e, :ev, now()) RETURNING id",
        {"e": endpoint_id, "ev": event_id},
    )
    schedule(ctx.db, ctx.tenant_id, kind=webhooks.DUE_KIND, unique_key=str(delivery_id), ref_id=delivery_id)
    return _delivery(ctx, delivery_id)


DELIVERY_SQL = (
    "SELECT d.id, d.endpoint_id, d.event_id, o.event_type, d.status, d.attempts, d.next_attempt_at, d.last_status_code, d.last_error, "
    "d.delivered_at, d.created_at FROM webhook_deliveries d JOIN outbox_events o ON o.tenant_id = d.tenant_id AND o.id = d.event_id "
    "WHERE d.tenant_id = :tenant_id"
)


def _delivery(ctx: TenantContext, delivery_id: UUID) -> DeliveryOut:
    return DeliveryOut(**one(ctx, f"{DELIVERY_SQL} AND d.id = :id", {"id": delivery_id}, "Delivery not found."))


@router.get(
    "/webhook-deliveries", response_model=Page[DeliveryOut], operation_id="listWebhookDeliveries", tags=["webhooks"]
)
def list_deliveries(
    paging: Paging,
    ctx: TenantContext = MANAGE,
    endpoint_id: UUID | None = None,
    status: Literal["pending", "delivered", "dead"] | None = None,
) -> Page[DeliveryOut]:
    where, params = "", {"e": endpoint_id, "s": status}
    if endpoint_id:
        where += " AND d.endpoint_id = :e"
    if status:
        where += " AND d.status = :s"
    total = scalar(ctx, f"SELECT count(*) FROM webhook_deliveries d WHERE d.tenant_id = :tenant_id{where}", params)
    rows = many(
        ctx,
        f"{DELIVERY_SQL}{where} ORDER BY d.created_at DESC LIMIT :limit OFFSET :offset",
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page[DeliveryOut](
        items=[DeliveryOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset
    )


@router.post(
    "/webhook-deliveries/{delivery_id}/replay",
    response_model=DeliveryOut,
    operation_id="replayWebhookDelivery",
    tags=["webhooks"],
)
def replay_delivery(delivery_id: UUID, ctx: TenantContext = MANAGE) -> DeliveryOut:
    """Send the same event again, with the same event ID. The receiver deduplicates on that ID."""
    current = _delivery(ctx, delivery_id)
    if current.status == "pending":
        raise ConflictError("This delivery is still being attempted.")
    execute(
        ctx,
        "UPDATE webhook_deliveries SET status = 'pending', attempts = 0, next_attempt_at = now(), last_error = NULL "
        "WHERE tenant_id = :tenant_id AND id = :id",
        {"id": delivery_id},
    )
    # The earlier due row for this delivery is finished; make it due again.
    schedule(ctx.db, ctx.tenant_id, kind=webhooks.DUE_KIND, unique_key=str(delivery_id), ref_id=delivery_id)
    execute(
        ctx,
        "UPDATE due_jobs SET status = 'pending', due_at = now(), attempts = 0, lease_token = NULL, lease_expires_at = NULL, "
        "finished_at = NULL WHERE tenant_id = :tenant_id AND kind = :k AND unique_key = :u",
        {"k": webhooks.DUE_KIND, "u": str(delivery_id)},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="webhook.replayed",
        target_type="webhook_delivery",
        target_id=str(delivery_id),
    )
    return _delivery(ctx, delivery_id)


__all__ = ["outbox", "router"]
