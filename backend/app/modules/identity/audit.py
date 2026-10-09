"""Append-only security audit events."""

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session


def record_audit(
    db: Session,
    *,
    tenant_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    data: dict[str, Any] | None = None,
    actor_type: str = "user",
    origin: str = "api",
) -> None:
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    api_key_id = db.info.get("api_key_id")
    if api_key_id and actor_type == "user":
        # Done through an API key: say so, and which one.
        actor_type, data = "integration", {**(data or {}), "api_key_id": api_key_id}
    db.execute(
        text(
            """
            INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_type,
                                      target_id, origin, correlation_id, data)
            VALUES (:tenant_id, :actor_type, :actor_id, :action, :target_type, :target_id,
                    :origin, :correlation_id, CAST(:data AS jsonb))
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "origin": origin,
            "correlation_id": correlation_id,
            "data": json.dumps(data or {}, default=str),
        },
    )
