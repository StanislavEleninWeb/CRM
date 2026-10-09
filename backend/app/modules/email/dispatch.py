"""Sending one approved email, reliably.

A send intent moves through explicit states:

    queued -> claimed -> dispatching -> provider_accepted
                    \\-> blocked / queued (limits)     \\-> unknown -> provider_accepted | failed (decided by a person)
                                                       \\-> failed / queued (provider said no)

Rules this module keeps:

* Every step is a short transaction. No transaction, and so no row lock, is open while the
  mailbox provider is being called.
* ``claimed`` means nothing has been sent, so an expired lease there is safe to take over.
  ``dispatching`` means the provider may have been called. An expired lease there becomes
  ``unknown`` and is reconciled against the mailbox. It is never sent again automatically.
* Eligibility is evaluated again immediately before ``dispatching`` is committed, under a lock
  shared with suppression and reply handling for that recipient. That commit is the boundary:
  an opt-out arriving after it cannot stop that message.
* ``provider_accepted`` means the mailbox provider took the message. It is not proof of delivery.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import outbox
from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger
from app.core.time import utcnow
from app.modules.email import eligibility, mime, sync
from app.modules.email.gmail import MailboxError

log = get_logger(__name__)
DUE_KIND = "email.send"
MAX_PROVIDER_RETRIES = 5
# After an ambiguous send the mailbox is checked at these intervals. The provider's search can lag.
RECONCILE_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
NEEDS_PERSON = "Not found in the mailbox after several checks. Look in the Sent folder and record what happened."


def unsubscribe_url(tenant_id: UUID, address: str) -> str:
    from app.modules.email.unsubscribe import make_token

    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/unsubscribe/{make_token(tenant_id, address)}"


def render(
    db: Session, tenant_id: UUID, draft: Any, mailbox: Any | None, stage: eligibility.Stage = "preview"
) -> tuple[str, eligibility.Decision]:
    """The final body and the eligibility decision for a draft as it stands now."""
    policy = eligibility.current_policy(db, tenant_id)
    identity = db.execute(text("SELECT sender_identity FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar()
    foot = eligibility.footer(
        dict(policy["rules"]) if policy else {},
        kind=draft["kind"],
        sender_identity=identity,
        unsubscribe_url=unsubscribe_url(tenant_id, draft["to_address"]),
    )
    body = eligibility.render(draft["body_text"], foot)
    decision = eligibility.evaluate(
        db,
        tenant_id,
        to_address=draft["to_address"],
        kind=draft["kind"],
        subject=draft["subject"],
        rendered_body=body,
        sender_address=mailbox["email_address"] if mailbox and mailbox["status"] != "revoked" else None,
        company_id=draft["company_id"],
        thread_id=draft["thread_id"],
        stage=stage,
    )
    return body, decision


def permitted(draft: Any, decision: eligibility.Decision) -> bool:
    """Allowed outright, or needing review for exactly the reasons the approver already looked at."""
    if decision.outcome == "allow":
        return True
    if decision.outcome == "block":
        return False
    reviewed = set(draft["reviewed_codes"] or [])
    return bool(reviewed) and {r["code"] for r in decision.reasons if r["level"] == "review"} <= reviewed


def refusal(decision: eligibility.Decision) -> str:
    wanted = "block" if decision.outcome == "block" else "review"
    return next((r["message"] for r in decision.reasons if r["level"] == wanted), "The outreach rules do not allow it.")


def release_draft(db: Session, tenant_id: UUID, draft_id: UUID) -> None:
    """A message that did not go out returns to an editable draft. Sending it later needs a fresh approval."""
    db.execute(
        text(
            "UPDATE email_drafts SET status = 'draft', version = version + 1, approved_version = NULL, approved_content_hash = NULL, "
            "approved_by = NULL, approved_at = NULL, review_note = NULL, reviewed_codes = '{}' "
            "WHERE tenant_id = :t AND id = :d AND status = 'queued'"
        ),
        {"t": tenant_id, "d": draft_id},
    )


def cancel_waiting(db: Session, tenant_id: UUID, condition: str, params: dict[str, Any], reason: str) -> int:
    """Stop messages that have not started sending. One already being sent cannot be recalled."""
    rows = db.execute(
        text(
            f"UPDATE send_intents SET state = 'cancelled', state_reason = :reason, lease_token = NULL, lease_expires_at = NULL "
            f"WHERE tenant_id = :t AND state IN ('queued', 'claimed') AND {condition} RETURNING id, draft_id"
        ),
        {"t": tenant_id, "reason": reason, **params},
    ).all()
    for row in rows:
        release_draft(db, tenant_id, row.draft_id)
        outbox.emit(
            db,
            tenant_id,
            "email.send.cancelled",
            subject_type="send_intent",
            subject_id=row.id,
            payload={"reason": reason},
        )
    return len(rows)


def _event(db: Session, tenant_id: UUID, intent_id: UUID, name: str, **payload: Any) -> None:
    outbox.emit(db, tenant_id, f"email.send.{name}", subject_type="send_intent", subject_id=intent_id, payload=payload)


def _tx(tenant_id: UUID) -> Any:
    return session_scope(RlsContext(tenant_id=tenant_id))


# --- worker side -----------------------------------------------------------------------------


def send_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    """Due-row handler. Releases the due row before doing anything slow."""
    if ref_id is None:
        return None
    db.commit()
    return process(tenant_id, ref_id)


def process(tenant_id: UUID, intent_id: UUID) -> datetime | None:
    """Advance one send intent as far as it can go now. Returns when to look again, or None when finished."""
    step, value = _claim(tenant_id, intent_id)
    if step == "wait":
        return value  # type: ignore[no-any-return]
    if step == "reconcile":
        return reconcile(tenant_id, intent_id)
    if step != "go":
        return None
    lease: UUID = value
    ready = _final_check(tenant_id, intent_id, lease)
    if ready is None:
        return None
    if isinstance(ready, datetime):
        return ready
    outcome = _call_provider(tenant_id, ready)
    return _record(tenant_id, intent_id, lease, ready, outcome)


def _claim(tenant_id: UUID, intent_id: UUID) -> tuple[str, Any]:
    lease_for = timedelta(seconds=get_settings().send_lease_seconds)
    with _tx(tenant_id) as db:
        intent = (
            db.execute(
                text("SELECT * FROM send_intents WHERE tenant_id = :t AND id = :id FOR UPDATE"),
                {"t": tenant_id, "id": intent_id},
            )
            .mappings()
            .one_or_none()
        )
        if intent is None:
            return "done", None
        now = utcnow()
        state, expired = intent["state"], bool(intent["lease_expires_at"] and intent["lease_expires_at"] <= now)
        if state == "queued" and intent["scheduled_for"] > now:
            return "wait", intent["scheduled_for"]
        if state == "queued" or (state == "claimed" and expired):
            # Nothing has been sent in either case, so taking over is safe.
            lease = uuid4()
            db.execute(
                text(
                    "UPDATE send_intents SET state = 'claimed', lease_token = :l, lease_expires_at = :e, attempts = attempts + 1 "
                    "WHERE tenant_id = :t AND id = :id"
                ),
                {"l": lease, "e": now + lease_for, "t": tenant_id, "id": intent_id},
            )
            return "go", lease
        if state in ("claimed", "dispatching") and not expired:
            return "wait", intent["lease_expires_at"]  # another worker has it
        if state == "dispatching":
            # The worker that was sending stopped. The provider may or may not have the message.
            db.execute(
                text(
                    "UPDATE send_intents SET state = 'unknown', lease_token = NULL, lease_expires_at = NULL, "
                    "state_reason = 'The worker stopped while sending; checking the mailbox.' WHERE tenant_id = :t AND id = :id"
                ),
                {"t": tenant_id, "id": intent_id},
            )
            _event(db, tenant_id, intent_id, "unknown", reason="worker stopped while sending")
            return "reconcile", None
        if state == "unknown":
            return "reconcile", None
        return "done", None


def _block(db: Session, tenant_id: UUID, intent: Any, state: str, reason: str) -> None:
    db.execute(
        text(
            "UPDATE send_intents SET state = :s, state_reason = :r, lease_token = NULL, lease_expires_at = NULL "
            "WHERE tenant_id = :t AND id = :id"
        ),
        {"s": state, "r": reason[:500], "t": tenant_id, "id": intent["id"]},
    )
    release_draft(db, tenant_id, intent["draft_id"])
    _event(db, tenant_id, intent["id"], state, reason=reason)


def _requeue(db: Session, tenant_id: UUID, intent_id: UUID, when: datetime, reason: str) -> datetime:
    db.execute(
        text(
            "UPDATE send_intents SET state = 'queued', scheduled_for = :w, state_reason = :r, lease_token = NULL, lease_expires_at = NULL "
            "WHERE tenant_id = :t AND id = :id"
        ),
        {"w": when, "r": reason, "t": tenant_id, "id": intent_id},
    )
    return when


def _final_check(tenant_id: UUID, intent_id: UUID, lease: UUID) -> dict[str, Any] | datetime | None:
    """Last look before sending. Commits ``dispatching`` and returns what to send, or says why not."""
    settings = get_settings()
    with _tx(tenant_id) as db:
        # Lock order matters. An opt-out, a suppression and a reply take the recipient lock first and
        # then touch the send row; taking them here in the other order could deadlock, and the
        # database might then abort the opt-out. The recipient never changes after the request.
        to_address = db.execute(
            text("SELECT to_address FROM send_intents WHERE tenant_id = :t AND id = :id"),
            {"t": tenant_id, "id": intent_id},
        ).scalar()
        if to_address is None:
            return None
        eligibility.lock_recipient(db, tenant_id, to_address)
        intent = (
            db.execute(
                text(
                    "SELECT * FROM send_intents WHERE tenant_id = :t AND id = :id AND state = 'claimed' AND lease_token = :l FOR UPDATE"
                ),
                {"t": tenant_id, "id": intent_id, "l": lease},
            )
            .mappings()
            .one_or_none()
        )
        if intent is None:
            return None  # cancelled or taken over since the claim
        draft = (
            db.execute(
                text("SELECT * FROM email_drafts WHERE tenant_id = :t AND id = :d FOR UPDATE"),
                {"t": tenant_id, "d": intent["draft_id"]},
            )
            .mappings()
            .one()
        )
        # Locking the mailbox row makes the limit checks below exact when several messages are due together.
        mailbox = (
            db.execute(
                text("SELECT * FROM mailboxes WHERE tenant_id = :t AND id = :m FOR UPDATE"),
                {"t": tenant_id, "m": intent["mailbox_id"]},
            )
            .mappings()
            .one()
        )
        if settings.email_dispatch == "off" or (settings.email_dispatch != "live" and not intent["dry_run"]):
            _block(db, tenant_id, intent, "blocked", "Sending is switched off on this installation.")
            return None
        from app.modules.billing import entitlements

        standing = entitlements.evaluate(db, tenant_id)
        if standing.restricted:
            _block(db, tenant_id, intent, "blocked", f"Not sent: {standing.reason}")
            return None
        if draft["version"] != intent["draft_version"] or draft["status"] != "queued":
            _block(db, tenant_id, intent, "blocked", "The message was changed after it was approved.")
            return None
        if mailbox["status"] == "revoked" or mailbox["token_ciphertext"] is None:
            _block(
                db,
                tenant_id,
                intent,
                "failed",
                "The mailbox is disconnected. Reconnect it and approve the message again.",
            )
            return None

        # Evaluated after the recipient lock was granted, so anything that committed first is seen.
        body, decision = render(db, tenant_id, draft, mailbox, "dispatch")
        eligibility.record(
            db,
            tenant_id,
            decision,
            to_address=intent["to_address"],
            kind=intent["kind"],
            stage="dispatch",
            draft_id=draft["id"],
            send_intent_id=intent_id,
        )
        if not permitted(draft, decision):
            _block(db, tenant_id, intent, "blocked", refusal(decision))
            return None
        if decision.content_hash != intent["content_hash"]:
            _block(
                db,
                tenant_id,
                intent,
                "blocked",
                "The outreach rules or sender details changed after approval. Approve the message again.",
            )
            return None

        now = utcnow()
        window = db.execute(
            text(
                "SELECT count(*) AS sent, min(dispatched_at) AS oldest, max(dispatched_at) AS newest FROM send_intents "
                "WHERE tenant_id = :t AND mailbox_id = :m AND NOT dry_run AND dispatched_at > :since "
                "AND state IN ('dispatching', 'provider_accepted', 'unknown')"
            ),
            {"t": tenant_id, "m": mailbox["id"], "since": now - timedelta(hours=24)},
        ).one()
        if not intent["dry_run"]:
            if window.sent >= mailbox["daily_send_limit"]:
                return _requeue(
                    db,
                    tenant_id,
                    intent_id,
                    window.oldest + timedelta(hours=24),
                    "Waiting: the daily sending limit was reached.",
                )
            gap = timedelta(seconds=mailbox["min_send_interval_seconds"])
            if window.newest is not None and window.newest + gap > now:
                return _requeue(db, tenant_id, intent_id, window.newest + gap, "Waiting: messages are spaced out.")

        reply_to, references, provider_thread = None, [], None
        if draft["thread_id"]:
            provider_thread = db.execute(
                text("SELECT provider_thread_id FROM email_threads WHERE tenant_id = :t AND id = :th"),
                {"t": tenant_id, "th": draft["thread_id"]},
            ).scalar()
            last = db.execute(
                text(
                    "SELECT rfc_message_id, reference_ids FROM email_messages WHERE tenant_id = :t AND thread_id = :th AND direction = 'inbound' "
                    "AND rfc_message_id IS NOT NULL ORDER BY sent_at DESC LIMIT 1"
                ),
                {"t": tenant_id, "th": draft["thread_id"]},
            ).one_or_none()
            if last is not None:
                reply_to, references = last.rfc_message_id, list(last.reference_ids or [])
        tenant_name = db.execute(text("SELECT name FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar()
        try:
            raw = mime.build_message(
                sender=mailbox["email_address"],
                sender_name=tenant_name,
                to=intent["to_address"],
                subject=draft["subject"],
                body=body,
                message_id=intent["rfc_message_id"],
                in_reply_to=reply_to,
                references=references,
                unsubscribe_url=unsubscribe_url(tenant_id, intent["to_address"])
                if intent["kind"] == "unsolicited"
                else None,
            )
        except ValueError as exc:
            _block(db, tenant_id, intent, "blocked", f"The message could not be built: {exc}.")
            return None
        # The boundary: once this commits, the message may leave. Nothing after this can call it back.
        db.execute(
            text(
                "UPDATE send_intents SET state = 'dispatching', dispatched_at = :now, lease_expires_at = :e, state_reason = NULL "
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"now": now, "e": now + timedelta(seconds=settings.send_lease_seconds), "t": tenant_id, "id": intent_id},
        )
        return {
            "raw": raw,
            "dry_run": intent["dry_run"],
            "mailbox": dict(mailbox),
            "provider_thread": provider_thread,
            "draft_id": draft["id"],
            "company_id": draft["company_id"],
            "lead_id": draft["lead_id"],
            "subject": draft["subject"],
            "attempts": intent["attempts"],
        }


def _call_provider(tenant_id: UUID, ready: dict[str, Any]) -> dict[str, Any]:
    """The only network step. No database transaction is open here."""
    if ready["dry_run"]:
        return {"result": "simulated"}
    try:
        token = sync.get_tokens().access_token(tenant_id, ready["mailbox"])
    except MailboxError as exc:
        # Before the send call: the message has certainly not gone.
        return {"result": "failed", "reason": str(exc), "revoked": exc.revoked}
    try:
        message_id, thread_id = sync.get_provider().send(token, ready["raw"], ready["provider_thread"])
    except MailboxError as exc:
        if exc.ambiguous:
            return {"result": "unknown", "reason": str(exc)}
        if exc.rate_limited:
            return {"result": "retry", "reason": str(exc), "after": exc.retry_after or 300}
        return {"result": "failed", "reason": str(exc), "revoked": exc.revoked}
    except Exception as exc:  # an unexpected failure during the call says nothing about whether it was sent
        log.error("email_send_unexpected", exc_info=exc)
        return {"result": "unknown", "reason": "The send call ended unexpectedly."}
    return {"result": "accepted", "message_id": message_id, "thread_id": thread_id}


def accept(
    db: Session, tenant_id: UUID, intent_id: UUID, message_id: str | None, thread_id: str | None, how: str
) -> bool:
    """Record that the provider has the message, with everything that follows from it."""
    row = db.execute(
        text(
            "UPDATE send_intents SET state = 'provider_accepted', provider_message_id = COALESCE(:pm, provider_message_id), provider_thread_id = COALESCE(:pt, provider_thread_id), "
            "accepted_at = now(), lease_token = NULL, lease_expires_at = NULL, state_reason = :how "
            "WHERE tenant_id = :t AND id = :id AND state IN ('dispatching', 'unknown') RETURNING draft_id, mailbox_id, to_address"
        ),
        {"pm": message_id, "pt": thread_id, "how": how, "t": tenant_id, "id": intent_id},
    ).one_or_none()
    if row is None:
        return False
    draft = db.execute(
        text(
            "UPDATE email_drafts SET status = 'sent' WHERE tenant_id = :t AND id = :d RETURNING company_id, lead_id, subject"
        ),
        {"t": tenant_id, "d": row.draft_id},
    ).one()
    if draft.company_id:
        db.execute(
            text(
                "INSERT INTO activities (tenant_id, kind, summary, actor_type, origin, company_id, lead_id, data) "
                "VALUES (:t, 'email.sent', :s, 'system', 'mailbox', :c, :l, CAST(:d AS jsonb))"
            ),
            {
                "t": tenant_id,
                "s": f"Email accepted by the mailbox provider: {draft.subject[:200]}",
                "c": draft.company_id,
                "l": draft.lead_id,
                "d": f'{{"send_intent_id": "{intent_id}"}}',
            },
        )
    if draft.lead_id:
        db.execute(
            text(
                "UPDATE leads SET outreach_status = 'emailed' WHERE tenant_id = :t AND id = :l AND outreach_status = 'not_contacted'"
            ),
            {"t": tenant_id, "l": draft.lead_id},
        )
    # Read the mailbox soon, so the sent message shows in the conversation.
    db.execute(
        text(
            "UPDATE due_jobs SET due_at = now() WHERE tenant_id = :t AND kind = 'mailbox.sync' AND ref_id = :m AND status = 'pending'"
        ),
        {"t": tenant_id, "m": row.mailbox_id},
    )
    _event(db, tenant_id, intent_id, "accepted", provider_message_id=message_id)
    return True


def _record(
    tenant_id: UUID, intent_id: UUID, lease: UUID, ready: dict[str, Any], outcome: dict[str, Any]
) -> datetime | None:
    result = outcome["result"]
    with _tx(tenant_id) as db:
        if result == "accepted":
            # True even if the lease ran out meanwhile: the provider has the message.
            accept(
                db,
                tenant_id,
                intent_id,
                outcome["message_id"],
                outcome["thread_id"],
                "Accepted by the mailbox provider.",
            )
            return None
        intent = (
            db.execute(
                text(
                    "SELECT id, draft_id, attempts FROM send_intents WHERE tenant_id = :t AND id = :id AND state = 'dispatching' "
                    "AND lease_token = :l FOR UPDATE"
                ),
                {"t": tenant_id, "id": intent_id, "l": lease},
            )
            .mappings()
            .one_or_none()
        )
        if intent is None:
            return None  # the lease ran out and recovery has already marked it unknown
        if result == "simulated":
            _block(db, tenant_id, intent, "simulated", "Dry run: every check passed and nothing was sent.")
            return None
        if result == "unknown":
            db.execute(
                text(
                    "UPDATE send_intents SET state = 'unknown', state_reason = :r, lease_token = NULL, lease_expires_at = NULL "
                    "WHERE tenant_id = :t AND id = :id"
                ),
                {
                    "r": f"{outcome['reason']} It may have been sent; checking the mailbox.",
                    "t": tenant_id,
                    "id": intent_id,
                },
            )
            _event(db, tenant_id, intent_id, "unknown", reason=outcome["reason"])
            return utcnow() + RECONCILE_DELAYS[0]
        if result == "retry" and intent["attempts"] < MAX_PROVIDER_RETRIES:
            # The provider refused it outright, so it was not sent and may be tried again later.
            db.execute(
                text("UPDATE send_intents SET dispatched_at = NULL WHERE tenant_id = :t AND id = :id"),
                {"t": tenant_id, "id": intent_id},
            )
            return _requeue(
                db,
                tenant_id,
                intent_id,
                utcnow() + timedelta(seconds=int(outcome["after"])),
                "Waiting: the provider asked to slow down.",
            )
        db.execute(
            text("UPDATE send_intents SET dispatched_at = NULL WHERE tenant_id = :t AND id = :id"),
            {"t": tenant_id, "id": intent_id},
        )
        _block(db, tenant_id, intent, "failed", outcome["reason"])
        if outcome.get("revoked"):
            db.execute(
                text(
                    "UPDATE mailboxes SET status = 'revoked', last_error = :e WHERE tenant_id = :t AND id = :m AND status <> 'revoked'"
                ),
                {"e": outcome["reason"], "t": tenant_id, "m": ready["mailbox"]["id"]},
            )
        return None


def reconcile(tenant_id: UUID, intent_id: UUID) -> datetime | None:
    """Find out whether an ambiguous send reached the mailbox. Never sends."""
    with _tx(tenant_id) as db:
        row = (
            db.execute(
                text(
                    "SELECT i.rfc_message_id, i.reconcile_attempts, i.mailbox_id FROM send_intents i "
                    "WHERE i.tenant_id = :t AND i.id = :id AND i.state = 'unknown'"
                ),
                {"t": tenant_id, "id": intent_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["reconcile_attempts"] >= len(RECONCILE_DELAYS):
            return None  # waiting for a person
        mailbox = dict(
            db.execute(
                text("SELECT * FROM mailboxes WHERE tenant_id = :t AND id = :m"),
                {"t": tenant_id, "m": row["mailbox_id"]},
            )
            .mappings()
            .one()
        )
    found: tuple[str, str] | None = None
    try:
        token = sync.get_tokens().access_token(tenant_id, mailbox)
        found = sync.get_provider().find_by_rfc_id(token, row["rfc_message_id"])
    except MailboxError as exc:
        log.warning("email_reconcile_failed", intent_id=str(intent_id), error=str(exc))
    with _tx(tenant_id) as db:
        if found is not None:
            accept(db, tenant_id, intent_id, found[0], found[1], "Found in the mailbox after an uncertain send.")
            return None
        # Not finding it proves nothing: the provider's search can lag behind. It stays unknown.
        attempts = db.execute(
            text(
                "UPDATE send_intents SET reconcile_attempts = reconcile_attempts + 1 WHERE tenant_id = :t AND id = :id AND state = 'unknown' "
                "RETURNING reconcile_attempts"
            ),
            {"t": tenant_id, "id": intent_id},
        ).scalar()
        if attempts is None:
            return None
        if attempts >= len(RECONCILE_DELAYS):
            db.execute(
                text(
                    "UPDATE send_intents SET state_reason = :r WHERE tenant_id = :t AND id = :id AND state = 'unknown'"
                ),
                {"r": NEEDS_PERSON, "t": tenant_id, "id": intent_id},
            )
            _event(db, tenant_id, intent_id, "needs_review", reason=NEEDS_PERSON)
            return None
        return utcnow() + RECONCILE_DELAYS[attempts]
