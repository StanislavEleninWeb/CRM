"""Retention, erasure on request, and workspace deletion.

What these do not do: reach into backups or the security log. Backups expire on their own
schedule (see docs/data-retention.md); a restored backup must have erasures re-applied from
the tombstones, which is why tombstones are kept.
"""

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger
from app.core.normalize import normalize_email
from app.core.storage import get_storage
from app.core.time import utcnow

log = get_logger(__name__)
PURGE_KIND = "retention.purge"
DELETE_KIND = "tenant.delete"
DELETION_GRACE = timedelta(days=7)
# Days each kind of record is kept. Chosen by the workspace within these bounds.
DEFAULTS = {
    "email_content_days": 730,
    "webhook_delivery_days": 30,
    "outbox_event_days": 90,
    "finished_job_days": 14,
    "idempotency_hours": 24,
    "audit_days": 730,
}
BOUNDS = {
    "email_content_days": (30, 3650),
    "webhook_delivery_days": (1, 365),
    "outbox_event_days": (7, 365),
    "finished_job_days": (1, 90),
    "idempotency_hours": (24, 168),
    "audit_days": (365, 3650),  # the security record is kept at least a year
}


def settings_for(db: Session, tenant_id: UUID) -> dict[str, int]:
    stored = db.execute(text("SELECT retention FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar() or {}
    return {key: int(stored.get(key, default)) for key, default in DEFAULTS.items()}


def tombstone_hash(tenant_id: UUID, kind: str, value: str) -> str:
    """Keyed, per tenant: the hash cannot be matched across workspaces or reversed with a public list."""
    key = hashlib.sha256(f"tombstone:{get_settings().session_secret}:{tenant_id}".encode()).digest()
    return hmac.new(key, f"{kind}:{value.strip().lower()}".encode(), hashlib.sha256).hexdigest()


def is_erased(db: Session, tenant_id: UUID, candidates: list[tuple[str, str | None]]) -> bool:
    hashes = [(kind, tombstone_hash(tenant_id, kind, value)) for kind, value in candidates if value]
    if not hashes:
        return False
    return bool(
        db.execute(
            text(
                "SELECT 1 FROM erasure_tombstones WHERE tenant_id = :t AND (kind, value_hash) IN "
                "(SELECT * FROM unnest(CAST(:kinds AS text[]), CAST(:hashes AS text[]))) LIMIT 1"
            ),
            {"t": tenant_id, "kinds": [k for k, _ in hashes], "hashes": [h for _, h in hashes]},
        ).first()
    )


def is_erased_for_test(tenant_id: UUID, candidates: list[tuple[str, str | None]]) -> bool:
    """The same check with its own short session. Used by checks that have no session open."""
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        return is_erased(db, tenant_id, candidates)


def erase_company(
    db: Session, tenant_id: UUID, company_id: UUID, *, reason: str, erased_by: UUID | None
) -> dict[str, int]:
    """Remove a business and everything recorded about it, and remember enough to keep it gone."""
    scope = {"t": tenant_id, "c": company_id}
    company = db.execute(
        text("SELECT domain, external_id FROM companies WHERE tenant_id = :t AND id = :c FOR UPDATE"), scope
    ).one_or_none()
    if company is None:
        return {}
    channels = db.execute(
        text(
            "SELECT kind, COALESCE(normalized_value, raw_value) AS value FROM contact_channels WHERE tenant_id = :t AND company_id = :c"
        ),
        scope,
    ).all()
    leads = db.execute(text("SELECT id, external_id FROM leads WHERE tenant_id = :t AND company_id = :c"), scope).all()
    listings = db.execute(
        text(
            "SELECT DISTINCT a.listing_id FROM lead_assessments a JOIN leads l ON l.tenant_id = a.tenant_id AND l.id = a.lead_id "
            "WHERE a.tenant_id = :t AND l.company_id = :c AND a.listing_id IS NOT NULL"
        ),
        scope,
    ).scalars()
    marks: set[tuple[str, str]] = set()
    emails: list[str] = []
    for channel in channels:
        if channel.kind == "email" and (address := normalize_email(channel.value or "")):
            marks.add(("email", address))
            emails.append(address)
        elif channel.kind == "phone" and channel.value:
            marks.add(("phone", channel.value))
    if company.domain:
        marks.add(("domain", company.domain))
    marks.update(
        ("external_id", value) for value in [company.external_id, *[lead.external_id for lead in leads]] if value
    )
    marks.update(("listing_id", value) for value in listings)

    counts: dict[str, int] = {}
    files = (
        db.execute(
            text("SELECT storage_key FROM attachments WHERE tenant_id = :t AND company_id = :c AND deleted_at IS NULL"),
            scope,
        )
        .scalars()
        .all()
    )
    lead_ids = [lead.id for lead in leads]
    for label, statement in (
        # Conversations and drafts are only loosely tied to the company, so they are removed by name.
        ("send requests", "DELETE FROM send_intents WHERE tenant_id = :t AND (to_address = ANY(CAST(:emails AS citext[])) OR draft_id IN "
                          "(SELECT id FROM email_drafts WHERE tenant_id = :t AND (company_id = :c OR lead_id = ANY(:leads))))"),
        ("drafts", "DELETE FROM email_drafts WHERE tenant_id = :t AND (company_id = :c OR lead_id = ANY(:leads) OR to_address = ANY(CAST(:emails AS citext[])))"),
        ("messages", "DELETE FROM email_messages WHERE tenant_id = :t AND thread_id IN (SELECT id FROM email_threads WHERE tenant_id = :t "
                     "AND (company_id = :c OR lead_id = ANY(:leads)))"),
        ("messages by address", "DELETE FROM email_messages WHERE tenant_id = :t AND (from_address = ANY(CAST(:emails AS citext[])) "
                                "OR to_addresses && CAST(:emails AS citext[]))"),
        ("conversations", "DELETE FROM email_threads th WHERE th.tenant_id = :t AND (th.company_id = :c OR th.lead_id = ANY(:leads) "
                          "OR NOT EXISTS (SELECT 1 FROM email_messages m WHERE m.tenant_id = th.tenant_id AND m.thread_id = th.id))"),
        ("recipient records", "DELETE FROM recipient_profiles WHERE tenant_id = :t AND address = ANY(CAST(:emails AS citext[]))"),
        ("consents", "DELETE FROM email_consents WHERE tenant_id = :t AND address = ANY(CAST(:emails AS citext[]))"),
        ("eligibility decisions", "SELECT eligibility_decisions_erase(CAST(:emails AS text[]))"),
        ("company", "DELETE FROM companies WHERE tenant_id = :t AND id = :c"),
    ):  # fmt: skip
        counts[label] = db.execute(text(statement), {**scope, "emails": emails, "leads": lead_ids}).rowcount or 0  # type: ignore[attr-defined]
    for address in emails:
        # They asked to be forgotten; the one thing kept is that this address must not be emailed.
        db.execute(
            text(
                "INSERT INTO email_suppressions (tenant_id, scope, value, reason, source, note) VALUES (:t, 'address', :a, 'manual', 'manual', "
                "'Erased on request') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant_id, "a": address},
        )
    for kind, value in marks:
        db.execute(
            text(
                "INSERT INTO erasure_tombstones (tenant_id, kind, value_hash, reason, erased_by) VALUES (:t, :k, :h, :r, :u) "
                "ON CONFLICT (tenant_id, kind, value_hash) DO NOTHING"
            ),
            {"t": tenant_id, "k": kind, "h": tombstone_hash(tenant_id, kind, value), "r": reason[:500], "u": erased_by},
        )
    storage = get_storage()
    for key in files:
        try:
            storage.delete(key)
        except Exception as exc:  # the row is gone either way; an orphaned object is reported, not ignored
            log.error("erasure_file_delete_failed", storage_key=key, exc_info=exc)
            counts["files not removed from storage"] = counts.get("files not removed from storage", 0) + 1
    counts["files"] = len(files)
    counts["identifiers remembered"] = len(marks)
    return counts


def purge(tenant_id: UUID) -> dict[str, int]:
    """Delete what has outlived the workspace's retention settings."""
    counts: dict[str, int] = {}
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        keep = settings_for(db, tenant_id)
        scope = {"t": tenant_id}
        for label, statement, params in (
            # The message stays as a line in the conversation; its content goes.
            ("email contents", "UPDATE email_messages SET body_text = NULL, body_html = NULL, snippet = NULL, attachments = '[]'::jsonb "
                               "WHERE tenant_id = :t AND sent_at < now() - make_interval(days => :n) AND (body_text IS NOT NULL OR body_html IS NOT NULL "
                               "OR snippet IS NOT NULL)", {"n": keep["email_content_days"]}),
            ("webhook deliveries", "DELETE FROM webhook_deliveries WHERE tenant_id = :t AND status <> 'pending' AND created_at < now() - make_interval(days => :n)",
             {"n": keep["webhook_delivery_days"]}),
            ("events", "DELETE FROM outbox_events o WHERE o.tenant_id = :t AND o.created_at < now() - make_interval(days => :n) AND NOT EXISTS "
                       "(SELECT 1 FROM webhook_deliveries d WHERE d.tenant_id = o.tenant_id AND d.event_id = o.id)", {"n": keep["outbox_event_days"]}),
            ("finished jobs", "DELETE FROM due_jobs WHERE tenant_id = :t AND status IN ('done', 'failed', 'cancelled') "
                              "AND finished_at < now() - make_interval(days => :n)", {"n": keep["finished_job_days"]}),
            ("idempotency records", "DELETE FROM idempotency_keys WHERE tenant_id = :t AND created_at < now() - make_interval(hours => :n)",
             {"n": keep["idempotency_hours"]}),
            ("expired support grants", "DELETE FROM support_grants WHERE tenant_id = :t AND expires_at < now() - interval '90 days'", {}),
        ):  # fmt: skip
            counts[label] = db.execute(text(statement), {**scope, **params}).rowcount or 0  # type: ignore[attr-defined]
    return counts


def purge_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    db.commit()
    purge(tenant_id)
    return utcnow() + timedelta(hours=24)


def delete_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    """Delete the workspace if deletion is still requested and due. Cancelling the request makes this a no-op."""
    keys = (
        db.execute(
            text(
                "SELECT storage_key FROM attachments WHERE tenant_id = :t AND deleted_at IS NULL UNION ALL "
                "SELECT storage_key FROM imports WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
        .scalars()
        .all()
    )
    due = db.execute(text("SELECT deletion_due_at FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar()
    if due is None:
        return None
    if due > utcnow():
        return due  # type: ignore[no-any-return]
    # Stored credentials and mailbox authorisations go first, so nothing can act for the workspace afterwards.
    db.execute(
        text(
            "UPDATE mailboxes SET token_ciphertext = NULL, token_nonce = NULL, status = 'revoked' WHERE tenant_id = :t"
        ),
        {"t": tenant_id},
    )
    db.execute(
        text("UPDATE provider_connections SET secret_ciphertext = NULL, secret_nonce = NULL WHERE tenant_id = :t"),
        {"t": tenant_id},
    )
    db.execute(
        text("UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) WHERE tenant_id = :t"), {"t": tenant_id}
    )
    deleted = db.execute(text("SELECT tenant_delete_due(:t)"), {"t": tenant_id}).scalar()
    if not deleted:
        return None
    db.commit()
    storage = get_storage()
    for key in keys:
        try:
            storage.delete(key)
        except Exception as exc:
            log.error("tenant_file_delete_failed", storage_key=key, exc_info=exc)
    log.info("tenant_deleted", tenant_id=str(tenant_id), files=len(keys))
    return None
