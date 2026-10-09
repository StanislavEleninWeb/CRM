"""Mailbox synchronisation.

The contract, in order of importance:
  * A push notification is only a trigger. The cursor is never taken from a notification.
  * Changes are read with ``history.list`` from the stored cursor, page by page. A page's
    messages are committed before the cursor moves past that page, so a crash replays the
    page instead of skipping it. Messages are stored idempotently.
  * An expired cursor (HTTP 404) switches to a resumable full synchronisation. The cursor
    used afterwards is the one captured *before* the full listing began, so nothing that
    arrives during it is missed.
  * One synchronisation per mailbox at a time, held by a lease rather than a row lock, so
    no database lock spans a network call.
  * A quiet mailbox is still reconciled on a timer; an overdue one is shown as degraded.
"""

import json
import random
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger
from app.core.secrets import Sealed, SecretError, get_keyring
from app.core.time import utcnow
from app.modules.email import mime
from app.modules.email.gmail import HistoryExpired, MailboxError, MailboxProvider, RawMessage

log = get_logger(__name__)
LEASE_SECONDS = 300
WATCH_ALERT_WINDOW = timedelta(hours=24)


class TokenSource:
    """Turns a mailbox's stored refresh token into an access token. Replaced in tests."""

    def access_token(self, tenant_id: UUID, mailbox: Any) -> str:
        from app.modules.email.oauth import refresh_access_token

        if mailbox["token_ciphertext"] is None:
            raise MailboxError("The mailbox has no stored authorisation.", revoked=True)
        try:
            refresh = get_keyring().open(
                Sealed(bytes(mailbox["token_ciphertext"]), bytes(mailbox["token_nonce"]), mailbox["token_key_version"]),
                tenant_id=tenant_id,
                provider=mailbox["provider"],
                connection_id=mailbox["id"],
            )
        except SecretError as exc:
            raise MailboxError("The stored mailbox authorisation cannot be read.", revoked=True) from exc
        return refresh_access_token(refresh)


_provider: MailboxProvider | None = None
_tokens: TokenSource = TokenSource()


def set_provider(provider: MailboxProvider | None, tokens: TokenSource | None = None) -> None:
    global _provider, _tokens
    _provider = provider
    if tokens is not None:
        _tokens = tokens


def get_provider() -> MailboxProvider:
    if _provider is not None:
        return _provider
    from app.modules.email.gmail import GmailProvider

    return GmailProvider()


def get_tokens() -> TokenSource:
    return _tokens


def _acquire(tenant_id: UUID, mailbox_id: UUID) -> tuple[Any, UUID] | None:
    """Take the per-mailbox lease in a short transaction. Returns None when another sync holds it."""
    lease = uuid4()
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        row = (
            db.execute(
                text(
                    "UPDATE mailboxes SET sync_lease_token = :l, sync_lease_until = now() + make_interval(secs => :s) "
                    "WHERE tenant_id = :t AND id = :m AND status <> 'revoked' AND (sync_lease_until IS NULL OR sync_lease_until < now()) "
                    "AND (backoff_until IS NULL OR backoff_until < now()) RETURNING *"
                ),
                {"l": lease, "s": LEASE_SECONDS, "t": tenant_id, "m": mailbox_id},
            )
            .mappings()
            .one_or_none()
        )
        return (row, lease) if row else None


def _release(tenant_id: UUID, mailbox_id: UUID, lease: UUID, **changes: Any) -> None:
    assignments = "".join(f", {column} = :{column}" for column in changes)
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        db.execute(
            text(
                f"UPDATE mailboxes SET sync_lease_token = NULL, sync_lease_until = NULL{assignments} "
                "WHERE tenant_id = :t AND id = :m AND sync_lease_token = :l"
            ),
            {"t": tenant_id, "m": mailbox_id, "l": lease, **changes},
        )


def sync_mailbox(tenant_id: UUID, mailbox_id: UUID) -> dict[str, Any]:
    """Bring one mailbox up to date. Safe to call at any time and from several workers at once."""
    acquired = _acquire(tenant_id, mailbox_id)
    if acquired is None:
        return {"skipped": "busy, backing off or revoked"}
    mailbox, lease = acquired
    provider = get_provider()
    stats = {
        "stored": 0,
        "pages": 0,
        "mode": "full" if mailbox["full_sync_required"] or not mailbox["history_cursor"] else "incremental",
    }
    try:
        token = _tokens.access_token(tenant_id, mailbox)
        if stats["mode"] == "incremental":
            try:
                _incremental(tenant_id, mailbox, provider, token, stats)
            except HistoryExpired:
                # The cursor is too old. Keep it out of use and rebuild from a full listing.
                with session_scope(RlsContext(tenant_id=tenant_id)) as db:
                    db.execute(
                        text(
                            "UPDATE mailboxes SET full_sync_required = true, full_sync_page_token = NULL, full_sync_started_cursor = NULL "
                            "WHERE tenant_id = :t AND id = :m"
                        ),
                        {"t": tenant_id, "m": mailbox_id},
                    )
                stats["mode"] = "full_after_expired_cursor"
                _full(tenant_id, mailbox_id, provider, token, stats)
        else:
            _full(tenant_id, mailbox_id, provider, token, stats)
    except MailboxError as exc:
        failures = mailbox["consecutive_failures"] + 1
        delay = exc.retry_after or min(60 * 2 ** min(failures, 6), 3600)
        _release(
            tenant_id,
            mailbox_id,
            lease,
            status="revoked" if exc.revoked else "degraded",
            last_error=str(exc)[:500],
            consecutive_failures=failures,
            backoff_until=None if exc.revoked else utcnow() + timedelta(seconds=delay),
        )
        return {**stats, "error": str(exc)}
    _release(
        tenant_id,
        mailbox_id,
        lease,
        status="active",
        last_error=None,
        consecutive_failures=0,
        backoff_until=None,
        last_synced_at=utcnow(),
    )
    return stats


def _incremental(tenant_id: UUID, mailbox: Any, provider: MailboxProvider, token: str, stats: dict[str, Any]) -> None:
    cursor, page_token = mailbox["history_cursor"], None
    while True:
        page = provider.list_history(token, cursor, page_token)
        fetched = _fetch_new(tenant_id, mailbox["id"], provider, token, page.message_ids)
        with session_scope(RlsContext(tenant_id=tenant_id)) as db:
            for raw in fetched:
                stats["stored"] += store_message(db, tenant_id, mailbox, raw)
            # Advance only past what this page covered, and only now that its messages are committed.
            advance_to = page.max_record_id if page.next_page_token else page.mailbox_history_id
            if advance_to:
                db.execute(
                    text(
                        "UPDATE mailboxes SET history_cursor = GREATEST(history_cursor::numeric, CAST(:c AS numeric))::text "
                        "WHERE tenant_id = :t AND id = :m"
                    ),
                    {"c": advance_to, "t": tenant_id, "m": mailbox["id"]},
                )
        stats["pages"] += 1
        if not page.next_page_token:
            return
        page_token = page.next_page_token


def _full(tenant_id: UUID, mailbox_id: UUID, provider: MailboxProvider, token: str, stats: dict[str, Any]) -> None:
    context = RlsContext(tenant_id=tenant_id)
    with session_scope(context) as db:
        mailbox = (
            db.execute(
                text("SELECT * FROM mailboxes WHERE tenant_id = :t AND id = :m"), {"t": tenant_id, "m": mailbox_id}
            )
            .mappings()
            .one()
        )
    if not mailbox["full_sync_started_cursor"]:
        _, history_id = provider.profile(token)
        with session_scope(context) as db:
            db.execute(
                text(
                    "UPDATE mailboxes SET full_sync_started_cursor = :c, full_sync_page_token = NULL WHERE tenant_id = :t AND id = :m"
                ),
                {"c": history_id, "t": tenant_id, "m": mailbox_id},
            )
        started, page_token = history_id, None
    else:
        started, page_token = (
            mailbox["full_sync_started_cursor"],
            mailbox["full_sync_page_token"],
        )  # resume where it stopped
    query = get_settings().gmail_sync_query
    while True:
        ids, next_token = provider.list_messages(token, query, page_token)
        fetched = _fetch_new(tenant_id, mailbox_id, provider, token, ids)
        with session_scope(context) as db:
            for raw in fetched:
                stats["stored"] += store_message(db, tenant_id, mailbox, raw)
            db.execute(
                text("UPDATE mailboxes SET full_sync_page_token = :p WHERE tenant_id = :t AND id = :m"),
                {"p": next_token, "t": tenant_id, "m": mailbox_id},
            )
        stats["pages"] += 1
        if not next_token:
            break
        page_token = next_token
    with session_scope(context) as db:
        db.execute(
            text(
                "UPDATE mailboxes SET history_cursor = :c, full_sync_required = false, full_sync_started_cursor = NULL, full_sync_page_token = NULL "
                "WHERE tenant_id = :t AND id = :m"
            ),
            {"c": started, "t": tenant_id, "m": mailbox_id},
        )


def _fetch_new(
    tenant_id: UUID, mailbox_id: UUID, provider: MailboxProvider, token: str, ids: list[str]
) -> list[RawMessage]:
    """Fetch messages not stored yet. No transaction is open while the provider is called."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        known = set(
            db.execute(
                text(
                    "SELECT provider_message_id FROM email_messages WHERE tenant_id = :t AND mailbox_id = :m "
                    "AND provider_message_id = ANY(:ids)"
                ),
                {"t": tenant_id, "m": mailbox_id, "ids": unique},
            ).scalars()
        )
    fetched = []
    for message_id in unique:
        if message_id in known:
            continue
        raw = provider.get_message(token, message_id)
        if raw is not None:
            fetched.append(raw)
    return fetched


def store_message(db: Session, tenant_id: UUID, mailbox: Any, raw: RawMessage) -> int:
    """Store one message with its thread, classification and links. Returns 1 if it was new."""
    headers = raw.headers
    from_address = mime.one_address(headers.get("from"))
    direction = (
        "outbound" if "SENT" in raw.label_ids or from_address == str(mailbox["email_address"]).lower() else "inbound"
    )
    subject = (headers.get("subject") or "")[:500]
    classification = "message"
    if direction == "inbound":
        classification = mime.classify(
            headers, from_address=from_address, subject=subject, content_type=raw.content_type
        )
    safe_html, remote = mime.sanitize_html(raw.html) if raw.html else ("", False)
    rfc_id = next(iter(mime.message_ids(headers.get("message-id"))), None)
    in_reply_to = next(iter(mime.message_ids(headers.get("in-reply-to"))), None)
    references = mime.message_ids(headers.get("references"))
    scope = {"t": tenant_id, "m": mailbox["id"]}

    thread = (
        db.execute(
            text(
                "INSERT INTO email_threads (tenant_id, mailbox_id, provider_thread_id, subject, last_message_at) VALUES (:t, :m, :pt, :s, :at) "
                "ON CONFLICT (tenant_id, mailbox_id, provider_thread_id) DO UPDATE SET last_message_at = GREATEST(email_threads.last_message_at, EXCLUDED.last_message_at) "
                "RETURNING id, company_id, lead_id, link_state, has_inbound"
            ),
            {**scope, "pt": raw.thread_id, "s": subject, "at": raw.internal_date},
        )
        .mappings()
        .one()
    )
    if direction == "inbound" and classification == "message":
        earlier_outbound = db.execute(
            text(
                "SELECT 1 FROM email_messages WHERE tenant_id = :t AND thread_id = :th AND direction = 'outbound' LIMIT 1"
            ),
            {"t": tenant_id, "th": thread["id"]},
        ).first()
        ours = (
            in_reply_to
            and db.execute(
                text(
                    "SELECT 1 FROM email_messages WHERE tenant_id = :t AND rfc_message_id = :r AND direction = 'outbound' LIMIT 1"
                ),
                {"t": tenant_id, "r": in_reply_to},
            ).first()
        )
        if earlier_outbound or ours:
            classification = "reply"
    inserted = db.execute(
        text(
            "INSERT INTO email_messages (tenant_id, mailbox_id, thread_id, provider_message_id, rfc_message_id, in_reply_to, reference_ids, "
            "direction, from_address, to_addresses, cc_addresses, subject, snippet, body_text, body_html, has_remote_content, attachments, "
            "classification, provider_labels, sent_at) VALUES (:t, :m, :th, :pm, :rfc, :irt, :refs, :dir, :from, :to, :cc, :subject, :snippet, "
            ":body, :html, :remote, CAST(:att AS jsonb), :cls, :labels, :at) "
            "ON CONFLICT (tenant_id, mailbox_id, provider_message_id) DO NOTHING RETURNING id"
        ),
        {
            **scope,
            "th": thread["id"],
            "pm": raw.id,
            "rfc": rfc_id,
            "irt": in_reply_to,
            "refs": references,
            "dir": direction,
            "from": from_address,
            "to": mime.addresses(headers.get("to")),
            "cc": mime.addresses(headers.get("cc")),
            "subject": subject,
            "snippet": raw.snippet[:300],
            "body": raw.text[:100_000],
            "html": safe_html,
            "remote": remote,
            "att": json.dumps(raw.attachments),
            "cls": classification,
            "labels": raw.label_ids,
            "at": raw.internal_date,
        },
    ).scalar_one_or_none()
    if inserted is None:
        return 0  # seen before: delivered twice, or replayed after a crash
    if direction == "outbound":
        db.execute(
            text(
                "UPDATE send_intents SET provider_message_id = COALESCE(provider_message_id, :pm), provider_thread_id = COALESCE(provider_thread_id, :pt) "
                "WHERE tenant_id = :t AND rfc_message_id = :rfc"
            ),
            {"t": tenant_id, "pm": raw.id, "pt": raw.thread_id, "rfc": rfc_id},
        )
        db.execute(
            text(
                "UPDATE email_messages SET send_intent_id = (SELECT id FROM send_intents WHERE tenant_id = :t AND rfc_message_id = :rfc) "
                "WHERE tenant_id = :t AND id = :id AND CAST(:rfc AS text) IS NOT NULL"
            ),
            {"t": tenant_id, "rfc": rfc_id, "id": inserted},
        )
        # A message this application sent belongs to the prospect it was written for.
        db.execute(
            text(
                "UPDATE email_threads th SET company_id = d.company_id, lead_id = d.lead_id, link_state = 'linked', link_note = NULL "
                "FROM send_intents i JOIN email_drafts d ON d.tenant_id = i.tenant_id AND d.id = i.draft_id "
                "WHERE th.tenant_id = :t AND th.id = :th AND i.tenant_id = :t AND i.rfc_message_id = :rfc "
                "AND th.link_state IN ('unmatched', 'conflict') AND d.company_id IS NOT NULL"
            ),
            {"t": tenant_id, "th": thread["id"], "rfc": rfc_id},
        )
        return 1
    _link_thread(db, tenant_id, thread, from_address)
    _inbound_effects(db, tenant_id, thread["id"], raw, classification, from_address)
    return 1


def _link_thread(db: Session, tenant_id: UUID, thread: Any, from_address: str | None) -> None:
    """Attach a conversation to a company when exactly one company owns the sender's address."""
    db.execute(
        text("UPDATE email_threads SET has_inbound = true WHERE tenant_id = :t AND id = :id"),
        {"t": tenant_id, "id": thread["id"]},
    )
    if thread["link_state"] in ("linked", "ignored") or not from_address:
        return
    matches = db.execute(
        text(
            "SELECT DISTINCT ch.company_id, ch.contact_id FROM contact_channels ch JOIN companies c ON c.tenant_id = ch.tenant_id AND c.id = ch.company_id "
            "WHERE ch.tenant_id = :t AND ch.kind = 'email' AND ch.normalized_value = :a AND c.merged_into_id IS NULL"
        ),
        {"t": tenant_id, "a": from_address},
    ).all()
    companies = {m.company_id for m in matches}
    if len(companies) == 1:
        company_id = next(iter(companies))
        lead_id = db.execute(
            text(
                "SELECT id FROM leads WHERE tenant_id = :t AND company_id = :c ORDER BY (status = 'converted'), created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "c": company_id},
        ).scalar_one_or_none()
        db.execute(
            text(
                "UPDATE email_threads SET company_id = :c, lead_id = :l, contact_id = :p, link_state = 'linked', link_note = 'sender address matches one company' "
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"c": company_id, "l": lead_id, "p": matches[0].contact_id, "t": tenant_id, "id": thread["id"]},
        )
    elif len(companies) > 1:
        # The same address on several companies: a person decides, the system does not guess.
        db.execute(
            text("UPDATE email_threads SET link_state = 'conflict', link_note = :n WHERE tenant_id = :t AND id = :id"),
            {"n": f"sender address is recorded on {len(companies)} companies", "t": tenant_id, "id": thread["id"]},
        )


def _inbound_effects(
    db: Session, tenant_id: UUID, thread_id: UUID, raw: RawMessage, classification: str, from_address: str | None
) -> None:
    thread = (
        db.execute(
            text("SELECT company_id, lead_id FROM email_threads WHERE tenant_id = :t AND id = :id"),
            {"t": tenant_id, "id": thread_id},
        )
        .mappings()
        .one()
    )
    if classification == "bounce":
        failed = mime.addresses(raw.headers.get("x-failed-recipients")) or list(
            db.execute(
                text(
                    "SELECT unnest(to_addresses) FROM email_messages WHERE tenant_id = :t AND thread_id = :th AND direction = 'outbound' "
                    "ORDER BY sent_at DESC LIMIT 1"
                ),
                {"t": tenant_id, "th": thread_id},
            ).scalars()
        )
        permanent = mime.is_permanent_bounce(raw.text, raw.headers)
        for address in failed:
            if permanent:
                from app.modules.email import eligibility

                eligibility.lock_recipient(db, tenant_id, address)
            db.execute(
                text(
                    "UPDATE send_intents SET delivery_evidence = 'bounced' WHERE tenant_id = :t AND to_address = :a AND state = 'provider_accepted'"
                ),
                {"t": tenant_id, "a": address},
            )
            if permanent:
                db.execute(
                    text(
                        "INSERT INTO email_suppressions (tenant_id, scope, value, reason, source, note) VALUES (:t, 'address', :a, 'permanent_bounce', "
                        "'mailbox', 'Reported undeliverable by the receiving server') ON CONFLICT DO NOTHING"
                    ),
                    {"t": tenant_id, "a": address},
                )
        return
    if classification not in ("reply", "message") or not from_address:
        return  # automatic replies change nothing; they wait for a person to look
    # A real reply stops any unsolicited message still waiting to go to this person.
    # An ordinary reply the user is writing in this conversation is not affected.
    from app.modules.email import dispatch, eligibility

    eligibility.lock_recipient(db, tenant_id, from_address)
    dispatch.cancel_waiting(
        db,
        tenant_id,
        "to_address = :a AND kind = 'unsolicited'",
        {"a": from_address},
        "The recipient replied before this was sent.",
    )
    db.execute(
        text(
            "UPDATE send_intents SET delivery_evidence = 'replied' WHERE tenant_id = :t AND to_address = :a AND state = 'provider_accepted' "
            "AND delivery_evidence = 'none'"
        ),
        {"t": tenant_id, "a": from_address},
    )
    if thread["lead_id"]:
        db.execute(
            text(
                "UPDATE leads SET outreach_status = 'replied' WHERE tenant_id = :t AND id = :l AND status <> 'converted'"
            ),
            {"t": tenant_id, "l": thread["lead_id"]},
        )
    if thread["company_id"]:
        db.execute(
            text(
                "INSERT INTO activities (tenant_id, kind, summary, actor_type, origin, company_id, lead_id, data) "
                "VALUES (:t, 'email.received', :s, 'system', 'mailbox', :c, :l, CAST(:d AS jsonb))"
            ),
            {
                "t": tenant_id,
                "s": f"Email received: {(raw.headers.get('subject') or '')[:200]}",
                "c": thread["company_id"],
                "l": thread["lead_id"],
                "d": json.dumps({"thread_id": str(thread_id), "classification": classification}),
            },
        )


def renew_watch(tenant_id: UUID, mailbox_id: UUID) -> dict[str, Any]:
    """Renew the push subscription. Renewing never moves the history cursor."""
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        mailbox = (
            db.execute(
                text("SELECT * FROM mailboxes WHERE tenant_id = :t AND id = :m AND status <> 'revoked'"),
                {"t": tenant_id, "m": mailbox_id},
            )
            .mappings()
            .one_or_none()
        )
    if mailbox is None:
        return {"skipped": "missing or revoked"}
    try:
        token = _tokens.access_token(tenant_id, mailbox)
        _, expires = get_provider().watch(token, get_settings().gmail_pubsub_topic)
    except MailboxError as exc:
        expiring = mailbox["watch_expires_at"] is None or mailbox["watch_expires_at"] - utcnow() < WATCH_ALERT_WINDOW
        with session_scope(RlsContext(tenant_id=tenant_id)) as db:
            db.execute(
                text(
                    "UPDATE mailboxes SET watch_renewal_failed_at = now(), last_error = :e, alert = CASE WHEN :expiring THEN 'watch_expiring' ELSE alert END, "
                    "status = CASE WHEN :revoked THEN 'revoked' WHEN :expiring THEN 'degraded' ELSE status END WHERE tenant_id = :t AND id = :m"
                ),
                {"e": str(exc)[:500], "expiring": expiring, "revoked": exc.revoked, "t": tenant_id, "m": mailbox_id},
            )
        return {"renewed": False, "alert": expiring, "error": str(exc)}
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        db.execute(
            text(
                "UPDATE mailboxes SET watch_expires_at = :e, watch_renewed_at = now(), watch_renewal_failed_at = NULL, "
                "alert = CASE WHEN alert = 'watch_expiring' THEN NULL ELSE alert END WHERE tenant_id = :t AND id = :m"
            ),
            {"e": expires, "t": tenant_id, "m": mailbox_id},
        )
    return {"renewed": True, "expires_at": expires}


def health(mailbox: Any) -> dict[str, Any]:
    """What a person needs to know about a connection, without reading logs."""
    interval = timedelta(minutes=get_settings().mailbox_reconcile_minutes)
    now = utcnow()
    problems: list[str] = []
    if mailbox["status"] == "revoked":
        problems.append("The connection was revoked. Reconnect the mailbox; its history is kept.")
    if mailbox["last_synced_at"] is None:
        problems.append("The mailbox has not been synchronised yet.")
    elif now - mailbox["last_synced_at"] > interval * 2:
        problems.append("Synchronisation is overdue, so recent replies may be missing.")
    if mailbox["alert"] == "watch_expiring":
        problems.append(
            "Push notifications could not be renewed and will stop soon. Replies will still arrive, more slowly."
        )
    if mailbox["backoff_until"] and mailbox["backoff_until"] > now:
        problems.append("The provider asked us to slow down; synchronisation will resume shortly.")
    if mailbox["full_sync_required"] and mailbox["last_synced_at"] is not None:
        problems.append("A full resynchronisation is in progress.")
    return {"ok": not problems and mailbox["status"] == "active", "problems": problems}


def sync_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    """Due-row handler: reconcile now, then again after the configured interval, quiet mailbox or not."""
    if ref_id is None:
        return None
    exists = db.execute(
        text("SELECT status FROM mailboxes WHERE tenant_id = :t AND id = :m"), {"t": tenant_id, "m": ref_id}
    ).scalar_one_or_none()
    if exists is None or exists == "revoked":
        return None
    db.commit()
    sync_mailbox(tenant_id, ref_id)
    return utcnow() + timedelta(minutes=get_settings().mailbox_reconcile_minutes)


def watch_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    """Due-row handler: renew the watch about once a day, with jitter so mailboxes do not renew together."""
    if ref_id is None:
        return None
    exists = db.execute(
        text("SELECT status FROM mailboxes WHERE tenant_id = :t AND id = :m"), {"t": tenant_id, "m": ref_id}
    ).scalar_one_or_none()
    if exists is None or exists == "revoked":
        return None
    db.commit()
    result = renew_watch(tenant_id, ref_id)
    # After a failure, try again within the hour rather than waiting a day.
    hours = 24 if result.get("renewed") else 1
    return utcnow() + timedelta(hours=hours, minutes=random.randint(-30, 30) if hours == 24 else 0)  # noqa: S311
