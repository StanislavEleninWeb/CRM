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
) -> None:
    """Store an event. It exists only if the surrounding transaction commits."""
    db.execute(
        text(
            "INSERT INTO outbox_events (tenant_id, event_type, subject_type, subject_id, payload) "
            "VALUES (:t, :e, :st, :sid, CAST(:p AS jsonb))"
        ),
        {"t": tenant_id, "e": event_type, "st": subject_type, "sid": subject_id, "p": json.dumps(payload, default=str)},
    )
