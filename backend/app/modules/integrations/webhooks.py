"""Outbound webhooks: signed, versioned, delivered at least once.

Each event is an ``outbox_events`` row written in the transaction that caused it. For every
endpoint that wants the event a ``webhook_deliveries`` row and a due row are written in that
same transaction, so an event that was committed is never lost and one that was rolled back
is never announced.

A receiver must treat the event ``id`` as the unit of work: a delivery can arrive more than
once (a retry after a timeout, or a replay requested by a person).
"""

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger
from app.core.secrets import Sealed, SecretError, get_keyring
from app.core.security import new_token
from app.core.time import utcnow
from app.modules.discovery.fetcher import FetchBlocked, guarded_client, validate_url

log = get_logger(__name__)
DUE_KIND = "webhook.deliver"
SECRET_PREFIX = "whsec_"  # noqa: S105
SIGNATURE_HEADER = "X-CRM-Signature"
# After the first attempt: one minute, five, thirty, two hours, six, a day. Then it is dead until replayed.
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=24),
)
TIMEOUT_SECONDS = 10.0
ROTATION_OVERLAP = timedelta(hours=24)
KNOWN_EVENTS = (
    "email.send.queued",
    "email.send.accepted",
    "email.send.unknown",
    "email.send.needs_review",
    "email.send.failed",
    "email.send.blocked",
    "email.send.cancelled",
    "email.send.simulated",
    "call.outcome_reported",
    "research_run.finished",
    "webhook.test",
)

_client_factory: Callable[[], httpx.Client] = guarded_client


def set_client_factory(factory: Callable[[], httpx.Client] | None) -> None:
    """Tests replace the network. Production always uses the guarded client."""
    global _client_factory
    _client_factory = factory or guarded_client


def check_destination(url: str) -> str:
    """Refuse destinations that are not public HTTPS addresses. Checked again at every delivery."""
    try:
        cleaned = validate_url(url)
    except FetchBlocked as exc:
        raise ValueError(str(exc)) from exc
    if not cleaned.lower().startswith("https://") and get_settings().environment not in ("development", "test"):
        raise ValueError("a webhook address must use https")
    return cleaned


def new_secret() -> str:
    return SECRET_PREFIX + new_token(32)


def seal_secret(tenant_id: UUID, endpoint_id: UUID, secret: str) -> Sealed:
    return get_keyring().seal(secret, tenant_id=tenant_id, provider="webhook", connection_id=endpoint_id)


def _open(tenant_id: UUID, endpoint_id: UUID, ciphertext: Any, nonce: Any, version: str) -> str:
    return get_keyring().open(
        Sealed(bytes(ciphertext), bytes(nonce), version),
        tenant_id=tenant_id,
        provider="webhook",
        connection_id=endpoint_id,
    )


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def signature_header(secrets: list[str], body: bytes, *, timestamp: int | None = None) -> str:
    """``t=<unix seconds>,v1=<hmac>``; a second ``v1`` is added while an old secret is still accepted."""
    at = timestamp or int(time.time())
    return ",".join([f"t={at}", *[f"v1={sign(secret, at, body)}" for secret in secrets]])


def verify(secret: str, header: str, body: bytes, *, tolerance_seconds: int = 300, now: int | None = None) -> bool:
    """What a receiver does. Provided so the examples and tests use the same check."""
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    stamps = [v for k, v in parts if k == "t"]
    if len(stamps) != 1 or not stamps[0].isdigit():
        return False
    timestamp = int(stamps[0])
    if abs((now or int(time.time())) - timestamp) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return any(hmac.compare_digest(expected, v) for k, v in parts if k == "v1")


def fan_out(db: Session, tenant_id: UUID, event_id: UUID, event_type: str) -> int:
    """Create one delivery per interested endpoint, in the caller's transaction."""
    from app.worker.due import schedule

    rows = db.execute(
        text(
            "INSERT INTO webhook_deliveries (tenant_id, endpoint_id, event_id, next_attempt_at) "
            "SELECT e.tenant_id, e.id, :ev, now() FROM webhook_endpoints e WHERE e.tenant_id = :t AND e.status = 'active' "
            "AND (e.event_types @> ARRAY['*'] OR e.event_types @> ARRAY[:type]) "
            "ON CONFLICT (tenant_id, endpoint_id, event_id) DO NOTHING RETURNING id"
        ),
        {"t": tenant_id, "ev": event_id, "type": event_type},
    ).all()
    for row in rows:
        schedule(db, tenant_id, kind=DUE_KIND, unique_key=str(row.id), ref_id=row.id)
    return len(rows)


def envelope(event: Any) -> bytes:
    return json.dumps(
        {
            "id": str(event["id"]),
            "type": event["event_type"],
            "api_version": event["api_version"],
            "created_at": event["created_at"].isoformat(),
            "tenant_id": str(event["tenant_id"]),
            "subject": {"type": event["subject_type"], "id": str(event["subject_id"]) if event["subject_id"] else None},
            "data": event["payload"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def deliver_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    if ref_id is None:
        return None
    db.commit()  # nothing is held while the receiver is being called
    return deliver(tenant_id, ref_id)


def deliver(tenant_id: UUID, delivery_id: UUID) -> datetime | None:
    """One attempt. Returns when to try again, or None when delivered, dead or no longer wanted."""
    context = RlsContext(tenant_id=tenant_id)
    with session_scope(context) as db:
        row = (
            db.execute(
                text(
                    "SELECT d.id, d.attempts, d.status, e.id AS endpoint_id, e.url, e.status AS endpoint_status, e.secret_ciphertext, "
                    "e.secret_nonce, e.secret_key_version, e.previous_secret_ciphertext, e.previous_secret_nonce, "
                    "e.previous_secret_key_version, e.previous_secret_expires_at, o.id AS event_id, o.tenant_id, o.event_type, "
                    "o.api_version, o.created_at, o.subject_type, o.subject_id, o.payload "
                    "FROM webhook_deliveries d JOIN webhook_endpoints e ON e.tenant_id = d.tenant_id AND e.id = d.endpoint_id "
                    "JOIN outbox_events o ON o.tenant_id = d.tenant_id AND o.id = d.event_id WHERE d.tenant_id = :t AND d.id = :id"
                ),
                {"t": tenant_id, "id": delivery_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None or row["status"] != "pending":
        return None
    if row["endpoint_status"] != "active":
        return utcnow() + timedelta(hours=1)  # paused: keep the event, look again later

    body = envelope({**row, "id": row["event_id"]})
    status_code: int | None = None
    error: str | None = None
    try:
        secrets = [
            _open(
                tenant_id, row["endpoint_id"], row["secret_ciphertext"], row["secret_nonce"], row["secret_key_version"]
            )
        ]
        if row["previous_secret_ciphertext"] and row["previous_secret_expires_at"] > utcnow():
            secrets.append(
                _open(
                    tenant_id,
                    row["endpoint_id"],
                    row["previous_secret_ciphertext"],
                    row["previous_secret_nonce"],
                    row["previous_secret_key_version"],
                )
            )
        url = check_destination(row["url"])
        with _client_factory() as client:
            # Redirects are not followed: a redirect is a failed delivery, so it cannot be used to reach another host.
            response = client.post(
                url,
                content=body,
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "SEWEB-CRM-Webhooks/1",
                    SIGNATURE_HEADER: signature_header(secrets, body),
                    "X-CRM-Event-Id": str(row["event_id"]),
                    "X-CRM-Event-Type": row["event_type"],
                    "X-CRM-Delivery-Id": str(row["id"]),
                },
            )
        status_code = response.status_code
        if not 200 <= status_code < 300:
            error = f"The receiver answered {status_code}."
    except SecretError:
        error = "The signing secret cannot be read. Rotate the endpoint's secret."
    except (ValueError, FetchBlocked) as exc:
        error = f"The address is not allowed: {exc}."
    except httpx.TimeoutException:
        error = "The receiver did not answer in time."
    except httpx.HTTPError as exc:
        error = f"The receiver could not be reached ({type(exc).__name__})."

    with session_scope(context) as db:
        attempts = row["attempts"] + 1
        if error is None:
            db.execute(
                text(
                    "UPDATE webhook_deliveries SET status = 'delivered', attempts = :a, delivered_at = now(), last_status_code = :s, "
                    "last_error = NULL, next_attempt_at = NULL WHERE tenant_id = :t AND id = :id AND status = 'pending'"
                ),
                {"a": attempts, "s": status_code, "t": tenant_id, "id": delivery_id},
            )
            db.execute(
                text(
                    "UPDATE webhook_endpoints SET consecutive_failures = 0, last_success_at = now(), last_error = NULL "
                    "WHERE tenant_id = :t AND id = :e"
                ),
                {"t": tenant_id, "e": row["endpoint_id"]},
            )
            return None
        again = utcnow() + RETRY_DELAYS[attempts - 1] if attempts <= len(RETRY_DELAYS) else None
        db.execute(
            text(
                "UPDATE webhook_deliveries SET status = :st, attempts = :a, last_status_code = :s, last_error = :e, next_attempt_at = :n "
                "WHERE tenant_id = :t AND id = :id AND status = 'pending'"
            ),
            {
                "st": "pending" if again else "dead",
                "a": attempts,
                "s": status_code,
                "e": error[:500],
                "n": again,
                "t": tenant_id,
                "id": delivery_id,
            },
        )
        db.execute(
            text(
                "UPDATE webhook_endpoints SET consecutive_failures = consecutive_failures + 1, last_failure_at = now(), last_error = :e "
                "WHERE tenant_id = :t AND id = :ep"
            ),
            {"e": error[:500], "t": tenant_id, "ep": row["endpoint_id"]},
        )
        return again
