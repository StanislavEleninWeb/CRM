"""Events recorded in the same transaction as the change they describe."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def emit(
    db: Session,
    tenant_id: UUID,
    event_type: str,
    *,
    subject_type: str,
    subject_id: UUID | None,
    payload: dict[str, Any],
) -> UUID:
    """Store an event and queue its deliveries. Both exist only if the surrounding transaction commits."""
    event_id = db.execute(
        text(
            "INSERT INTO outbox_events (tenant_id, event_type, subject_type, subject_id, payload) "
            "VALUES (:t, :e, :st, :sid, CAST(:p AS jsonb)) RETURNING id"
        ),
        {"t": tenant_id, "e": event_type, "st": subject_type, "sid": subject_id, "p": json.dumps(payload, default=str)},
    ).scalar_one()
    from app.modules.integrations import webhooks

    webhooks.fan_out(db, tenant_id, event_id, event_type)
    return event_id  # type: ignore[no-any-return]
