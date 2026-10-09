"""Provider connections, health, budgets and the usage dashboard."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.engine import RowMapping

from app.core.deps import TenantContext, tenant_with
from app.core.errors import AppError, ConflictError
from app.core.pagination import Page, PageParams, page_params
from app.core.secrets import Sealed, SecretError, get_keyring, hint
from app.core.time import today_in, utcnow
from app.modules.crm.common import ValidationFailed, execute, many, one, scalar
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.modules.providers import budgets
from app.modules.providers.adapters import available_adapters, get_adapter

router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
MANAGE = tenant_with(Permission.INTEGRATIONS_MANAGE)
REPORTS = tenant_with(Permission.REPORTS_READ)

# Columns that are safe to return. Ciphertext, nonce and anything derived from the secret are never selected.
PUBLIC_COLUMNS = """
    id, provider, purpose, label, access_mode, status, secret_hint, secret_key_version, config, verification,
    last_checked_at, last_ok_at, last_error, rate_limited_until, consecutive_failures, revoked_at, created_at, updated_at
"""


class ProviderUnavailable(AppError):
    status_code = 409
    code = "provider_unavailable"


class AdapterOut(BaseModel):
    name: str
    purpose: str
    label: str
    access_modes: list[str]
    is_local_test_adapter: bool


class ConnectionIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    label: str = Field(min_length=1, max_length=120)
    access_mode: Literal["byok", "platform", "oauth"] = "byok"
    credential: str | None = Field(
        default=None, min_length=8, max_length=4000, description="Write-only. Never returned."
    )
    config: dict[str, str | int | bool] = Field(default_factory=dict)


class CredentialIn(BaseModel):
    model_config = {"extra": "forbid"}
    credential: str = Field(min_length=8, max_length=4000, description="Write-only. Never returned.")


class ConnectionOut(BaseModel):
    id: UUID
    provider: str
    purpose: str
    label: str
    access_mode: str
    status: Literal["pending", "active", "error", "revoked"]
    has_credential: bool
    credential_hint: str | None
    encryption_key_version: str | None
    config: dict[str, Any]
    verification: str
    is_local_test_adapter: bool
    last_checked_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    rate_limited_until: datetime | None
    consecutive_failures: int
    revoked_at: datetime | None
    created_at: datetime


class BudgetIn(BaseModel):
    model_config = {"extra": "forbid"}
    scope: Literal["research", "model", "discovery", "all"] = "research"
    period: Literal["month", "total"] = "month"
    limit_amount: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    max_concurrent_runs: int = Field(default=1, ge=1, le=50)
    allow_overage: bool = False


class BudgetOut(BaseModel):
    id: UUID
    scope: str
    period: str
    period_start: Any
    currency: str
    limit_amount: Decimal
    spent_amount: Decimal
    reserved_amount: Decimal
    available_amount: Decimal
    max_concurrent_runs: int
    allow_overage: bool
    overrun: bool


class ReservationOut(BaseModel):
    id: UUID
    budget_id: UUID
    state: Literal["reserved", "settled", "released", "unknown"]
    purpose: str
    run_ref: str | None
    reserved_amount: Decimal
    actual_amount: Decimal | None
    currency: str
    note: str | None
    created_at: datetime
    settled_at: datetime | None


class ResolveIn(BaseModel):
    model_config = {"extra": "forbid"}
    charged: bool
    actual_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    note: str = Field(min_length=3, max_length=500)


class UsageSummary(BaseModel):
    currency: str
    period_start: Any
    verified_amount: Decimal = Field(description="Reported by the provider or confirmed on an invoice")
    estimated_amount: Decimal = Field(
        description="Calculated by this application from list prices; may differ from the bill"
    )
    unknown_reserved_amount: Decimal = Field(description="Held for work whose outcome is unknown")
    reserved_amount: Decimal
    by_provider: list[dict[str, Any]]
    entries: int


def _connection(row: RowMapping) -> ConnectionOut:
    data = dict(row)
    adapter = get_adapter(data["provider"])
    return ConnectionOut(
        **{k: data[k] for k in ConnectionOut.model_fields if k in data},
        has_credential=data["secret_key_version"] is not None,
        credential_hint=data["secret_hint"],
        encryption_key_version=data["secret_key_version"],
        is_local_test_adapter=bool(adapter and adapter.is_fake),
    )


def _get(ctx: TenantContext, connection_id: UUID, *, lock: bool = False) -> RowMapping:
    return one(
        ctx,
        f"SELECT {PUBLIC_COLUMNS} FROM provider_connections WHERE tenant_id = :tenant_id AND id = :id {'FOR UPDATE' if lock else ''}",
        {"id": connection_id},
        "Connection not found.",
    )


def _seal(ctx: TenantContext, connection_id: UUID, provider: str, credential: str) -> dict[str, Any]:
    try:
        sealed = get_keyring().seal(credential, tenant_id=ctx.tenant_id, provider=provider, connection_id=connection_id)
    except SecretError as exc:
        raise ProviderUnavailable("Credentials cannot be stored because no encryption key is configured.") from exc
    return {"ct": sealed.ciphertext, "nonce": sealed.nonce, "kv": sealed.key_version, "hint": hint(credential)}


def load_credential(ctx: TenantContext, connection_id: UUID) -> tuple[RowMapping, str | None]:
    """For server-side use only: the connection and its decrypted credential. Never returned by an endpoint."""
    row = one(
        ctx,
        "SELECT * FROM provider_connections WHERE tenant_id = :tenant_id AND id = :id",
        {"id": connection_id},
        "Connection not found.",
    )
    if row["secret_ciphertext"] is None:
        return row, None
    try:
        secret = get_keyring().open(
            Sealed(
                ciphertext=bytes(row["secret_ciphertext"]),
                nonce=bytes(row["secret_nonce"]),
                key_version=row["secret_key_version"],
            ),
            tenant_id=ctx.tenant_id,
            provider=row["provider"],
            connection_id=connection_id,
        )
    except SecretError as exc:
        raise ProviderUnavailable("The stored credential cannot be read. Reconnect this provider.") from exc
    return row, secret


def ensure_usable(ctx: TenantContext, connection_id: UUID) -> tuple[RowMapping, str]:
    """The connection and credential for dispatching work, or a clear error saying why not."""
    row, secret = load_credential(ctx, connection_id)
    if row["status"] == "revoked":
        raise ProviderUnavailable("This connection was revoked. Reconnect it to continue.")
    if secret is None:
        raise ProviderUnavailable("No credential is stored for this connection.")
    if row["rate_limited_until"] and row["rate_limited_until"] > utcnow():
        raise ProviderUnavailable("The provider is rate limiting requests. Try again later.")
    return row, secret


def record_health(
    ctx: TenantContext,
    connection_id: UUID,
    *,
    ok: bool,
    message: str = "",
    revoked: bool = False,
    rate_limited_for_seconds: int | None = None,
) -> None:
    """Update health after any provider call. Failures back off exponentially, capped at one hour."""
    if ok:
        execute(
            ctx,
            "UPDATE provider_connections SET status = 'active', last_checked_at = now(), last_ok_at = now(), last_error = NULL, "
            "consecutive_failures = 0, rate_limited_until = NULL WHERE tenant_id = :tenant_id AND id = :id AND status <> 'revoked'",
            {"id": connection_id},
        )
        return
    failures = (
        scalar(
            ctx,
            "SELECT consecutive_failures FROM provider_connections WHERE tenant_id = :tenant_id AND id = :id",
            {"id": connection_id},
        )
        or 0
    )
    backoff = rate_limited_for_seconds or min(30 * 2**failures, 3600)
    execute(
        ctx,
        "UPDATE provider_connections SET status = :s, last_checked_at = now(), last_error = :e, "
        "consecutive_failures = consecutive_failures + 1, rate_limited_until = :until, "
        "revoked_at = CASE WHEN :s = 'revoked' THEN now() ELSE revoked_at END WHERE tenant_id = :tenant_id AND id = :id",
        {
            "s": "revoked" if revoked else "error",
            "e": message[:500],
            "until": None if revoked else utcnow() + timedelta(seconds=backoff),
            "id": connection_id,
        },
    )


# --- connections -----------------------------------------------------------------------------


@router.get(
    "/provider-adapters", response_model=list[AdapterOut], operation_id="listProviderAdapters", tags=["providers"]
)
def list_adapters(ctx: TenantContext = MANAGE) -> list[AdapterOut]:
    return [
        AdapterOut(
            name=a.name,
            purpose=a.purpose,
            label=a.label,
            access_modes=list(a.access_modes),
            is_local_test_adapter=a.is_fake,
        )
        for a in available_adapters()
    ]


@router.get(
    "/provider-connections",
    response_model=list[ConnectionOut],
    operation_id="listProviderConnections",
    tags=["providers"],
)
def list_connections(ctx: TenantContext = MANAGE) -> list[ConnectionOut]:
    rows = many(
        ctx,
        f"SELECT {PUBLIC_COLUMNS} FROM provider_connections WHERE tenant_id = :tenant_id ORDER BY purpose, lower(label)",
    )
    return [_connection(r) for r in rows]


@router.post(
    "/provider-connections",
    response_model=ConnectionOut,
    status_code=201,
    operation_id="createProviderConnection",
    tags=["providers"],
)
def create_connection(body: ConnectionIn, ctx: TenantContext = MANAGE) -> ConnectionOut:
    adapter = get_adapter(body.provider)
    if adapter is None:
        raise ValidationFailed("That provider is not available in this environment.")
    if body.access_mode not in adapter.access_modes:
        raise ValidationFailed(f"{adapter.label} does not support {body.access_mode} access.")
    if body.access_mode == "byok" and not body.credential:
        raise ValidationFailed("An API key is required for bring-your-own-key access.")
    connection_id = uuid4()
    sealed = (
        _seal(ctx, connection_id, body.provider, body.credential)
        if body.credential
        else {"ct": None, "nonce": None, "kv": None, "hint": None}
    )
    execute(
        ctx,
        "INSERT INTO provider_connections (id, tenant_id, provider, purpose, label, access_mode, secret_ciphertext, secret_nonce, "
        "secret_key_version, secret_hint, config, created_by) VALUES (:id, :tenant_id, :provider, :purpose, :label, :mode, :ct, "
        ":nonce, :kv, :hint, CAST(:config AS jsonb), :u)",
        {
            "id": connection_id,
            "provider": body.provider,
            "purpose": adapter.purpose,
            "label": body.label,
            "mode": body.access_mode,
            "config": json.dumps(body.config),
            "u": ctx.user_id,
            **sealed,
        },
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="provider.connected",
        target_type="provider_connection",
        target_id=str(connection_id),
        data={"provider": body.provider, "access_mode": body.access_mode},
    )
    return _connection(_get(ctx, connection_id))


@router.put(
    "/provider-connections/{connection_id}/credential",
    response_model=ConnectionOut,
    operation_id="replaceProviderCredential",
    tags=["providers"],
)
def replace_credential(connection_id: UUID, body: CredentialIn, ctx: TenantContext = MANAGE) -> ConnectionOut:
    """Reconnect or rotate. History that refers to this connection is kept."""
    current = _get(ctx, connection_id, lock=True)
    sealed = _seal(ctx, connection_id, current["provider"], body.credential)
    execute(
        ctx,
        "UPDATE provider_connections SET secret_ciphertext = :ct, secret_nonce = :nonce, secret_key_version = :kv, secret_hint = :hint, "
        "status = 'pending', revoked_at = NULL, last_error = NULL, consecutive_failures = 0, rate_limited_until = NULL "
        "WHERE tenant_id = :tenant_id AND id = :id",
        {"id": connection_id, **sealed},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="provider.credential_replaced",
        target_type="provider_connection",
        target_id=str(connection_id),
    )
    return _connection(_get(ctx, connection_id))


@router.post(
    "/provider-connections/{connection_id}/reseal",
    response_model=ConnectionOut,
    operation_id="resealProviderCredential",
    tags=["providers"],
)
def reseal_credential(connection_id: UUID, ctx: TenantContext = MANAGE) -> ConnectionOut:
    """Re-encrypt the stored credential with the current key version (used during key rotation)."""
    row, secret = load_credential(ctx, connection_id)
    if secret is not None and row["secret_key_version"] != get_keyring().current_version:
        sealed = _seal(ctx, connection_id, row["provider"], secret)
        execute(
            ctx,
            "UPDATE provider_connections SET secret_ciphertext = :ct, secret_nonce = :nonce, secret_key_version = :kv "
            "WHERE tenant_id = :tenant_id AND id = :id",
            {"id": connection_id, "ct": sealed["ct"], "nonce": sealed["nonce"], "kv": sealed["kv"]},
        )
    return _connection(_get(ctx, connection_id))


@router.post(
    "/provider-connections/{connection_id}/check",
    response_model=ConnectionOut,
    operation_id="checkProviderConnection",
    tags=["providers"],
)
def check_connection(connection_id: UUID, ctx: TenantContext = MANAGE) -> ConnectionOut:
    row, secret = load_credential(ctx, connection_id)
    if row["status"] == "revoked":
        raise ProviderUnavailable("This connection was revoked. Reconnect it to continue.")
    adapter = get_adapter(row["provider"])
    if adapter is None:
        raise ProviderUnavailable("That provider is not available in this environment.")
    result = adapter.validate(secret, row["config"])
    record_health(
        ctx,
        connection_id,
        ok=result.ok,
        message=result.message,
        revoked=result.revoked,
        rate_limited_for_seconds=result.rate_limited_for_seconds,
    )
    if result.ok and not adapter.is_fake:
        execute(
            ctx,
            "UPDATE provider_connections SET verification = 'verified_live' WHERE tenant_id = :tenant_id AND id = :id",
            {"id": connection_id},
        )
    elif result.ok:
        execute(
            ctx,
            "UPDATE provider_connections SET verification = 'verified_locally' WHERE tenant_id = :tenant_id AND id = :id",
            {"id": connection_id},
        )
    return _connection(_get(ctx, connection_id))


@router.delete(
    "/provider-connections/{connection_id}",
    response_model=ConnectionOut,
    operation_id="revokeProviderConnection",
    tags=["providers"],
)
def revoke_connection(connection_id: UUID, ctx: TenantContext = MANAGE) -> ConnectionOut:
    """Revoke and erase the stored credential. The connection record and its usage history remain."""
    _get(ctx, connection_id, lock=True)
    execute(
        ctx,
        "UPDATE provider_connections SET status = 'revoked', revoked_at = now(), secret_ciphertext = NULL, secret_nonce = NULL, "
        "secret_key_version = NULL, secret_hint = NULL WHERE tenant_id = :tenant_id AND id = :id",
        {"id": connection_id},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="provider.revoked",
        target_type="provider_connection",
        target_id=str(connection_id),
    )
    return _connection(_get(ctx, connection_id))


# --- budgets ---------------------------------------------------------------------------------


def _budget(row: RowMapping) -> BudgetOut:
    data = dict(row)
    available = max(data["limit_amount"] - data["spent_amount"] - data["reserved_amount"], Decimal("0"))
    return BudgetOut(
        **{k: data[k] for k in BudgetOut.model_fields if k in data},
        available_amount=(Decimal("0") if data["overrun"] else available).quantize(Decimal("0.0001")),
    )


@router.get("/budgets", response_model=list[BudgetOut], operation_id="listBudgets", tags=["budgets"])
def list_budgets(ctx: TenantContext = REPORTS) -> list[BudgetOut]:
    rows = many(ctx, "SELECT * FROM budgets WHERE tenant_id = :tenant_id ORDER BY period_start DESC, scope LIMIT 100")
    return [_budget(r) for r in rows]


@router.put("/budgets", response_model=BudgetOut, operation_id="setBudget", tags=["budgets"])
def set_budget(body: BudgetIn, ctx: TenantContext = tenant_with(Permission.TENANT_BILLING)) -> BudgetOut:
    """Set the limit for the current period. A limit cannot be set below what is already spent or held."""
    tenant = one(ctx, "SELECT currency, timezone FROM tenants WHERE id = :tenant_id", {}, "Workspace not found.")
    start = budgets.period_start(body.period, today_in(tenant["timezone"]))
    current = scalar(
        ctx,
        "SELECT spent_amount + reserved_amount FROM budgets WHERE tenant_id = :tenant_id AND scope = :s AND period = :p "
        "AND period_start = :d FOR UPDATE",
        {"s": body.scope, "p": body.period, "d": start},
    )
    if current is not None and not body.allow_overage and body.limit_amount < current:
        raise ConflictError(
            f"{current} {tenant['currency']} is already spent or held, so the limit cannot be lower than that."
        )
    row = one(
        ctx,
        "INSERT INTO budgets (tenant_id, scope, period, period_start, currency, limit_amount, max_concurrent_runs, allow_overage) "
        "VALUES (:tenant_id, :scope, :period, :d, :cur, :limit_amount, :max_concurrent_runs, :allow_overage) "
        "ON CONFLICT (tenant_id, scope, period, period_start) DO UPDATE SET limit_amount = EXCLUDED.limit_amount, "
        "max_concurrent_runs = EXCLUDED.max_concurrent_runs, allow_overage = EXCLUDED.allow_overage, "
        "overrun = budgets.spent_amount + budgets.reserved_amount > EXCLUDED.limit_amount AND NOT EXCLUDED.allow_overage RETURNING *",
        {**body.model_dump(), "d": start, "cur": tenant["currency"]},
        "Budget not found.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="budget.set",
        target_type="budget",
        target_id=str(row["id"]),
        data={
            "scope": body.scope,
            "period": body.period,
            "limit": str(body.limit_amount),
            "allow_overage": body.allow_overage,
        },
    )
    return _budget(row)


@router.get(
    "/budget-reservations", response_model=Page[ReservationOut], operation_id="listBudgetReservations", tags=["budgets"]
)
def list_reservations(
    paging: Paging,
    ctx: TenantContext = REPORTS,
    state: Literal["reserved", "settled", "released", "unknown"] | None = None,
) -> Page[ReservationOut]:
    condition = "tenant_id = :tenant_id" + (" AND state = :state" if state else "")
    params = {"state": state} if state else {}
    total = scalar(ctx, f"SELECT count(*) FROM budget_reservations WHERE {condition}", params)
    rows = many(
        ctx,
        f"SELECT * FROM budget_reservations WHERE {condition} ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset",
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[ReservationOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.post(
    "/budget-reservations/{reservation_id}/resolve",
    response_model=ReservationOut,
    operation_id="resolveBudgetReservation",
    tags=["budgets"],
)
def resolve_reservation(
    reservation_id: UUID, body: ResolveIn, ctx: TenantContext = tenant_with(Permission.TENANT_BILLING)
) -> ReservationOut:
    """Reconcile work whose outcome was unknown, after checking the provider's own records."""
    if body.charged and body.actual_amount is None:
        raise ValidationFailed("Enter the amount the provider charged.")
    provider = (
        scalar(
            ctx,
            "SELECT c.provider FROM budget_reservations r LEFT JOIN provider_connections c ON c.tenant_id = r.tenant_id "
            "AND c.id = r.connection_id WHERE r.tenant_id = :tenant_id AND r.id = :id",
            {"id": reservation_id},
        )
        or "unknown"
    )
    budgets.resolve_unknown(
        ctx.db,
        ctx.tenant_id,
        reservation_id,
        actual=body.actual_amount if body.charged else None,
        note=body.note,
        provider=provider,
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="budget.reservation_resolved",
        target_type="budget_reservation",
        target_id=str(reservation_id),
        data={"charged": body.charged, "amount": str(body.actual_amount) if body.charged else None},
    )
    return ReservationOut(
        **one(
            ctx,
            "SELECT * FROM budget_reservations WHERE tenant_id = :tenant_id AND id = :id",
            {"id": reservation_id},
            "Reservation not found.",
        )
    )


@router.get("/usage/summary", response_model=UsageSummary, operation_id="getUsageSummary", tags=["budgets"])
def usage_summary(ctx: TenantContext = REPORTS) -> UsageSummary:
    """This month's usage. Verified and estimated amounts are kept apart: an estimate is not a bill."""
    tenant = one(ctx, "SELECT currency, timezone FROM tenants WHERE id = :tenant_id", {}, "Workspace not found.")
    start = budgets.period_start("month", today_in(tenant["timezone"]))
    params = {"start": start}
    totals = one(
        ctx,
        "SELECT COALESCE(sum(amount) FILTER (WHERE cost_basis IN ('provider_reported', 'verified_invoice')), 0)::numeric(14, 4) AS verified, "
        "COALESCE(sum(amount) FILTER (WHERE cost_basis IN ('estimated', 'unknown')), 0)::numeric(14, 4) AS estimated, count(*) AS entries "
        "FROM usage_ledger WHERE tenant_id = :tenant_id AND occurred_at >= :start",
        params,
        "No usage.",
    )
    held = one(
        ctx,
        "SELECT COALESCE(sum(reserved_amount) FILTER (WHERE state = 'unknown'), 0)::numeric(14, 4) AS unknown, "
        "COALESCE(sum(reserved_amount) FILTER (WHERE state = 'reserved'), 0)::numeric(14, 4) AS reserved FROM budget_reservations WHERE tenant_id = :tenant_id",
        {},
        "No reservations.",
    )
    by_provider = many(
        ctx,
        "SELECT provider, cost_basis, billed_to, sum(amount) AS amount, count(*) AS entries FROM usage_ledger "
        "WHERE tenant_id = :tenant_id AND occurred_at >= :start GROUP BY 1, 2, 3 ORDER BY 1, 2",
        params,
    )
    return UsageSummary(
        currency=tenant["currency"],
        period_start=start,
        verified_amount=totals["verified"],
        estimated_amount=totals["estimated"],
        unknown_reserved_amount=held["unknown"],
        reserved_amount=held["reserved"],
        by_provider=[dict(r) for r in by_provider],
        entries=totals["entries"],
    )
