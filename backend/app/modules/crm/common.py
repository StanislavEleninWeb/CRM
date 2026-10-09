"""Shared helpers for CRM endpoints."""

import json
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from app.core.deps import TenantContext
from app.core.errors import AppError, NotFoundError, PermissionDeniedError
from app.modules.identity.permissions import Permission

ENTITY_TABLES = {"company": "companies", "contact": "contacts", "lead": "leads", "deal": "deals"}
MAX_CUSTOM_FIELDS_PER_ENTITY = 30


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


def one(ctx: TenantContext, sql: str, params: dict[str, Any], missing: str) -> RowMapping:
    row = ctx.db.execute(text(sql), {"tenant_id": ctx.tenant_id, **params}).mappings().one_or_none()
    if row is None:
        raise NotFoundError(missing)
    return row


def many(ctx: TenantContext, sql: str, params: dict[str, Any] | None = None) -> list[RowMapping]:
    return list(ctx.db.execute(text(sql), {"tenant_id": ctx.tenant_id, **(params or {})}).mappings())


def scalar(ctx: TenantContext, sql: str, params: dict[str, Any] | None = None) -> Any:
    return ctx.db.execute(text(sql), {"tenant_id": ctx.tenant_id, **(params or {})}).scalar()


def execute(ctx: TenantContext, sql: str, params: dict[str, Any] | None = None) -> int:
    """Run a statement and return the number of rows it changed."""
    result = ctx.db.execute(text(sql), {"tenant_id": ctx.tenant_id, **(params or {})})
    return int(getattr(result, "rowcount", 0) or 0)


def exists(ctx: TenantContext, table: str, row_id: UUID | None, label: str) -> None:
    """Explicit check so a bad reference is a clear 422, not a constraint error."""
    if row_id is None:
        return
    found = scalar(
        ctx,
        f"SELECT 1 FROM {table} WHERE tenant_id = :tenant_id AND id = :id",
        {"id": row_id},
    )
    if not found:
        raise ValidationFailed(f"{label} was not found in this workspace.")


def check_owner(ctx: TenantContext, owner_user_id: UUID | None) -> None:
    """Assigning a record to someone else needs the assign permission."""
    if owner_user_id is None:
        return
    if owner_user_id != ctx.user_id and Permission.CRM_ASSIGN not in ctx.permissions:
        raise PermissionDeniedError("You can only assign records to yourself.")
    is_member = scalar(
        ctx,
        "SELECT 1 FROM memberships WHERE tenant_id = :tenant_id AND user_id = :u",
        {"u": owner_user_id},
    )
    if not is_member:
        raise ValidationFailed("The owner must be a member of this workspace.")


def log_activity(
    ctx: TenantContext,
    kind: str,
    summary: str,
    *,
    company_id: UUID | None = None,
    contact_id: UUID | None = None,
    lead_id: UUID | None = None,
    deal_id: UUID | None = None,
    data: dict[str, Any] | None = None,
    origin: str = "api",
) -> None:
    ctx.db.execute(
        text(
            """
            INSERT INTO activities (tenant_id, kind, summary, actor_user_id, actor_type, origin, correlation_id,
                                    data, company_id, contact_id, lead_id, deal_id)
            VALUES (:tenant_id, :kind, :summary, :actor, :actor_type, :origin, :correlation_id,
                    CAST(:data AS jsonb), :company_id, :contact_id, :lead_id, :deal_id)
            """
        ),
        {
            "tenant_id": ctx.tenant_id,
            "kind": kind,
            "summary": summary[:500],
            "actor": ctx.user_id,
            "actor_type": "integration" if ctx.principal.api_key_id else "user",
            "origin": "api_key" if ctx.principal.api_key_id and origin == "api" else origin,
            "correlation_id": structlog.contextvars.get_contextvars().get("correlation_id"),
            "data": json.dumps(data or {}, default=str),
            "company_id": company_id,
            "contact_id": contact_id,
            "lead_id": lead_id,
            "deal_id": deal_id,
        },
    )


def set_clause(changes: dict[str, Any]) -> str:
    """``a = :a, b = :b`` for column names that come from a pydantic model, never from input."""
    return ", ".join(f"{column} = :{column}" for column in changes)


def validate_custom(ctx: TenantContext, entity_type: str, custom: dict[str, Any] | None) -> str:
    """Check custom values against the tenant's field definitions. Returns JSON text."""
    if not custom:
        return "{}"
    definitions = {
        row["key"]: row
        for row in many(
            ctx,
            "SELECT key, field_type, options FROM custom_field_definitions "
            "WHERE tenant_id = :tenant_id AND entity_type = :e",
            {"e": entity_type},
        )
    }
    cleaned: dict[str, Any] = {}
    for key, value in custom.items():
        definition = definitions.get(key)
        if definition is None:
            raise ValidationFailed(f"Unknown custom field: {key}")
        if value is None:
            continue
        kind = definition["field_type"]
        ok = (
            (kind == "text" and isinstance(value, str) and len(value) <= 2000)
            or (kind == "number" and isinstance(value, int | float | Decimal) and not isinstance(value, bool))
            or (kind == "boolean" and isinstance(value, bool))
            or (kind == "select" and value in definition["options"])
            or (kind == "date" and isinstance(value, str) and _is_iso_date(value))
        )
        if not ok:
            raise ValidationFailed(f"Invalid value for custom field: {key}")
        cleaned[key] = value
    return json.dumps(cleaned)


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def order_by(sort: str | None, allowed: dict[str, str], default: str) -> str:
    """Translate a ``field`` / ``-field`` request into a whitelisted ORDER BY."""
    if not sort:
        return default
    descending = sort.startswith("-")
    column = allowed.get(sort.lstrip("-"))
    if column is None:
        raise ValidationFailed(f"Cannot sort by {sort.lstrip('-')}. Allowed: {', '.join(sorted(allowed))}.")
    return f"{column} {'DESC' if descending else 'ASC'} NULLS LAST, id"
