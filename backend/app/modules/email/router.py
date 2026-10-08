"""Mailboxes, conversations, drafts and the outreach rules that govern email."""

import base64
import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import redis
from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.deps import TenantContext, get_redis, tenant_with
from app.core.errors import ConflictError, UnauthenticatedError
from app.core.normalize import normalize_email
from app.core.pagination import Page, PageParams, page_params
from app.core.secrets import get_keyring
from app.core.security import constant_time_equal, new_token, set_cookie
from app.core.time import utcnow
from app.modules.crm.common import ValidationFailed, execute, exists, log_activity, many, one, scalar
from app.modules.email import eligibility, sync
from app.modules.email.oauth import GoogleAuth, ensure_tenant_may_connect, get_google_auth
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.worker.due import schedule

router = APIRouter()
public_router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
READ = tenant_with(Permission.CRM_READ)
DRAFT = tenant_with(Permission.OUTREACH_DRAFT)
MANAGE = tenant_with(Permission.INTEGRATIONS_MANAGE)
SETTINGS = tenant_with(Permission.TENANT_SETTINGS)
GMAIL_STATE_COOKIE = "crm_gmail_state"


class MailboxOut(BaseModel):
    id: UUID
    provider: str
    email_address: str
    mode: str
    status: Literal["pending", "active", "degraded", "error", "revoked"]
    verification: str
    scopes: list[str]
    last_synced_at: datetime | None
    last_notification_at: datetime | None
    watch_expires_at: datetime | None
    full_sync_required: bool
    last_error: str | None
    healthy: bool
    problems: list[str]
    can_read_replies: bool


class GmailAvailability(BaseModel):
    available: bool
    reason: str | None
    internal_domain: str | None


class MessageOut(BaseModel):
    id: UUID
    direction: Literal["inbound", "outbound"]
    classification: str
    from_address: str | None
    to_addresses: list[str]
    subject: str | None
    snippet: str | None
    body_text: str | None
    body_html: str | None = Field(description="Sanitised: scripts, forms and remote images are removed")
    has_remote_content: bool
    attachments: list[dict[str, Any]] = Field(description="Names and sizes only. Attachments are not downloaded.")
    sent_at: datetime | None


class ThreadOut(BaseModel):
    id: UUID
    mailbox_id: UUID
    subject: str | None
    company_id: UUID | None
    lead_id: UUID | None
    link_state: Literal["linked", "unmatched", "conflict", "ignored"]
    link_note: str | None
    last_message_at: datetime | None
    has_inbound: bool
    messages: list[MessageOut] = []


class LinkThread(BaseModel):
    company_id: UUID | None = None
    lead_id: UUID | None = None
    ignore: bool = False


class DraftIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    kind: Literal["unsolicited", "reply", "requested"] = "unsolicited"
    lead_id: UUID | None = None
    thread_id: UUID | None = None
    to_address: str | None = Field(default=None, max_length=254)
    subject: str | None = Field(default=None, max_length=300)
    body_text: str | None = Field(default=None, max_length=20000)


class DraftUpdate(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    to_address: str | None = Field(default=None, max_length=254)
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    body_text: str | None = Field(default=None, min_length=1, max_length=20000)


class EligibilityOut(BaseModel):
    outcome: Literal["allow", "review", "block"]
    reasons: list[dict[str, str]]
    checks: dict[str, Any]
    policy_version: str | None
    rendered_body: str = Field(description="Exactly what would be sent, including the part the sender cannot edit")


class DraftOut(BaseModel):
    id: UUID
    kind: str
    lead_id: UUID | None
    company_id: UUID | None
    thread_id: UUID | None
    mailbox_id: UUID | None
    to_address: str
    subject: str
    body_text: str
    version: int
    status: Literal["draft", "approved", "queued", "sent", "cancelled"]
    approved_version: int | None
    approved_at: datetime | None
    created_at: datetime
    eligibility: EligibilityOut


class OutreachPolicyOut(BaseModel):
    id: UUID
    jurisdiction: str
    version: str
    status: Literal["draft", "approved", "retired"]
    rules: dict[str, Any]
    source_checked_on: date | None
    source_note: str | None
    approved_at: datetime | None
    approval_note: str | None
    sender_identity: str | None
    opt_out_register: dict[str, Any]


class PolicyApprove(BaseModel):
    model_config = {"extra": "forbid"}
    approval_note: str = Field(
        min_length=20, max_length=2000, description="Who reviewed the current legal text and register process, and when"
    )
    source_checked_on: date
    register_max_age_days: int = Field(ge=1, le=90)
    label_text: str = Field(min_length=3, max_length=200)
    sole_trader: Literal["review", "treat_as_legal_person", "block_without_consent"] = "review"
    requires_register_check: bool = True


class SenderIdentity(BaseModel):
    sender_identity: str = Field(
        min_length=10, max_length=500, description="Legal name and contact details shown in every outgoing message"
    )


class SuppressionIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    scope: Literal["address", "domain"] = "address"
    value: str = Field(min_length=3, max_length=254)
    reason: Literal["opt_out", "permanent_bounce", "complaint", "manual"] = "manual"
    note: str | None = Field(default=None, max_length=500)


class SuppressionOut(BaseModel):
    id: UUID
    scope: str
    value: str
    reason: str
    source: str
    note: str | None
    created_at: datetime
    lifted_at: datetime | None
    lift_note: str | None


class LiftIn(BaseModel):
    lift_note: str = Field(min_length=5, max_length=500)


class ProfileIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    address: str = Field(max_length=254)
    legal_form: Literal["legal_person", "sole_trader", "natural_person", "unknown"]
    context: Literal["business", "consumer", "unknown"]
    evidence: str | None = Field(default=None, max_length=1000)
    evidence_url: str | None = Field(default=None, max_length=1000)


class ConsentIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    address: str = Field(max_length=254)
    kind: Literal["requested_follow_up", "opt_in"]
    scope: str = Field(min_length=3, max_length=500)
    source: str = Field(min_length=2, max_length=200)
    evidence: str | None = Field(default=None, max_length=1000)
    obtained_at: datetime


# --- mailboxes -------------------------------------------------------------------------------

MAILBOX_COLUMNS = (
    "id, provider, email_address, mode, status, verification, scopes, last_synced_at, last_notification_at, watch_expires_at, "
    "full_sync_required, last_error, alert, backoff_until"
)


def _mailbox(row: Any) -> MailboxOut:
    state = sync.health(row)
    data = dict(row)
    return MailboxOut(
        **{k: data[k] for k in MailboxOut.model_fields if k in data},
        healthy=state["ok"],
        problems=state["problems"],
        can_read_replies="https://www.googleapis.com/auth/gmail.readonly" in data["scopes"],
    )


def active_mailbox(ctx: TenantContext) -> Any | None:
    rows = many(
        ctx,
        "SELECT * FROM mailboxes WHERE tenant_id = :tenant_id AND status IN ('active', 'degraded', 'pending') ORDER BY created_at LIMIT 1",
    )
    return rows[0] if rows else None


@router.get("/mailboxes", response_model=list[MailboxOut], operation_id="listMailboxes", tags=["mailboxes"])
def list_mailboxes(ctx: TenantContext = READ) -> list[MailboxOut]:
    return [
        _mailbox(r)
        for r in many(ctx, f"SELECT {MAILBOX_COLUMNS} FROM mailboxes WHERE tenant_id = :tenant_id ORDER BY created_at")
    ]


@router.get(
    "/mailboxes/gmail/availability",
    response_model=GmailAvailability,
    operation_id="getGmailAvailability",
    tags=["mailboxes"],
)
def gmail_availability(ctx: TenantContext = READ) -> GmailAvailability:
    settings = get_settings()
    try:
        ensure_tenant_may_connect(settings, str(ctx.tenant_id))
    except Exception as exc:
        return GmailAvailability(
            available=False,
            reason=str(getattr(exc, "message", exc)),
            internal_domain=settings.gmail_internal_domain or None,
        )
    return GmailAvailability(available=True, reason=None, internal_domain=settings.gmail_internal_domain)


@router.get(
    "/mailboxes/gmail/connect",
    operation_id="connectGmail",
    status_code=307,
    response_class=RedirectResponse,
    tags=["mailboxes"],
)
def connect_gmail(
    auth: Annotated[GoogleAuth, Depends(get_google_auth)],
    store: Annotated[redis.Redis, Depends(get_redis)],
    ctx: TenantContext = MANAGE,
) -> RedirectResponse:
    ensure_tenant_may_connect(get_settings(), str(ctx.tenant_id))
    state, nonce = new_token(), new_token()
    store.set(
        f"crm:gmail:state:{state}",
        json.dumps({"tenant_id": str(ctx.tenant_id), "user_id": str(ctx.user_id), "nonce": nonce}),
        ex=600,
    )
    response = RedirectResponse(auth.authorization_url(state=state, nonce=nonce), status_code=307)
    set_cookie(response, GMAIL_STATE_COOKIE, state, max_age=600)
    return response


@router.get(
    "/mailboxes/gmail/callback",
    operation_id="gmailCallback",
    status_code=303,
    response_class=RedirectResponse,
    tags=["mailboxes"],
)
def gmail_callback(
    request: Request,
    auth: Annotated[GoogleAuth, Depends(get_google_auth)],
    store: Annotated[redis.Redis, Depends(get_redis)],
    ctx: TenantContext = MANAGE,
    code: Annotated[str | None, Query(max_length=2000)] = None,
    state: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    settings = get_settings()
    bound = request.cookies.get(GMAIL_STATE_COOKIE, "")
    if not code or not state or not bound or not constant_time_equal(state, bound):
        raise UnauthenticatedError("The mailbox connection could not be verified. Start again.")
    pending_raw = store.getdel(f"crm:gmail:state:{state}")
    if not isinstance(pending_raw, str):
        raise UnauthenticatedError("The mailbox connection expired. Start again.")
    pending = json.loads(pending_raw)
    # The tenant comes from the server-side record made at the start, and must still be this session's tenant.
    if pending["tenant_id"] != str(ctx.tenant_id) or pending["user_id"] != str(ctx.user_id):
        raise UnauthenticatedError(
            "The mailbox connection was started in a different workspace or by a different user."
        )
    ensure_tenant_may_connect(settings, str(ctx.tenant_id))
    account = auth.exchange(code=code, nonce=pending["nonce"])

    existing = scalar(
        ctx,
        "SELECT id FROM mailboxes WHERE tenant_id = :tenant_id AND provider = 'gmail' AND email_address = :e",
        {"e": account.email},
    )
    mailbox_id = existing or uuid4()
    sealed = get_keyring().seal(
        account.refresh_token, tenant_id=ctx.tenant_id, provider="gmail", connection_id=mailbox_id
    )
    values = {
        "id": mailbox_id,
        "e": account.email,
        "hd": account.hosted_domain,
        "scopes": sorted(account.scopes),
        "ct": sealed.ciphertext,
        "n": sealed.nonce,
        "kv": sealed.key_version,
        "u": ctx.user_id,
    }
    if existing:
        # Reconnecting keeps the mailbox record, its conversations and its position in the history.
        execute(
            ctx,
            "UPDATE mailboxes SET status = 'pending', scopes = :scopes, hosted_domain = :hd, token_ciphertext = :ct, token_nonce = :n, "
            "token_key_version = :kv, last_error = NULL, backoff_until = NULL, consecutive_failures = 0, alert = NULL "
            "WHERE tenant_id = :tenant_id AND id = :id",
            values,
        )
    else:
        execute(
            ctx,
            "INSERT INTO mailboxes (id, tenant_id, provider, email_address, owner_user_id, mode, hosted_domain, scopes, token_ciphertext, "
            "token_nonce, token_key_version) VALUES (:id, :tenant_id, 'gmail', :e, :u, 'internal', :hd, :scopes, :ct, :n, :kv)",
            values,
        )
        execute(
            ctx,
            "INSERT INTO mailbox_routes (tenant_id, mailbox_id, provider, email_address) VALUES (:tenant_id, :id, 'gmail', :e)",
            values,
        )
    schedule_mailbox_jobs(ctx, mailbox_id)
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="mailbox.connected",
        target_type="mailbox",
        target_id=str(mailbox_id),
        data={"provider": "gmail", "reconnected": bool(existing), "scopes": sorted(account.scopes)},
    )
    response = RedirectResponse(f"{settings.public_base_url.rstrip('/')}/integrations", status_code=303)
    response.delete_cookie(GMAIL_STATE_COOKIE, path="/")
    return response


def schedule_mailbox_jobs(ctx: TenantContext, mailbox_id: UUID) -> None:
    for kind in ("mailbox.sync", "mailbox.watch"):
        execute(
            ctx,
            "DELETE FROM due_jobs WHERE tenant_id = :tenant_id AND kind = :k AND ref_id = :m AND status IN ('done', 'failed', 'cancelled')",
            {"k": kind, "m": mailbox_id},
        )
        schedule(ctx.db, ctx.tenant_id, kind=kind, unique_key=str(mailbox_id), ref_id=mailbox_id)
        execute(
            ctx,
            "UPDATE due_jobs SET status = 'pending', due_at = now(), lease_token = NULL WHERE tenant_id = :tenant_id AND kind = :k "
            "AND ref_id = :m AND status = 'pending'",
            {"k": kind, "m": mailbox_id},
        )


@router.post("/mailboxes/{mailbox_id}/sync", response_model=MailboxOut, operation_id="syncMailbox", tags=["mailboxes"])
def sync_now(mailbox_id: UUID, ctx: TenantContext = MANAGE) -> MailboxOut:
    exists(ctx, "mailboxes", mailbox_id, "The mailbox")
    ctx.db.commit()
    sync.sync_mailbox(ctx.tenant_id, mailbox_id)
    return _mailbox(
        one(
            ctx,
            f"SELECT {MAILBOX_COLUMNS} FROM mailboxes WHERE tenant_id = :tenant_id AND id = :id",
            {"id": mailbox_id},
            "Mailbox not found.",
        )
    )


@router.delete(
    "/mailboxes/{mailbox_id}", response_model=MailboxOut, operation_id="disconnectMailbox", tags=["mailboxes"]
)
def disconnect_mailbox(mailbox_id: UUID, ctx: TenantContext = MANAGE) -> MailboxOut:
    """Stop using a mailbox and erase its stored authorisation. Conversations already synchronised are kept."""
    row = one(
        ctx,
        "UPDATE mailboxes SET status = 'revoked', token_ciphertext = NULL, token_nonce = NULL, token_key_version = NULL "
        f"WHERE tenant_id = :tenant_id AND id = :id RETURNING {MAILBOX_COLUMNS}",
        {"id": mailbox_id},
        "Mailbox not found.",
    )
    execute(
        ctx,
        "UPDATE due_jobs SET status = 'cancelled', finished_at = now() WHERE tenant_id = :tenant_id AND ref_id = :id AND kind LIKE 'mailbox.%' "
        "AND status IN ('pending', 'claimed')",
        {"id": mailbox_id},
    )
    execute(
        ctx,
        "UPDATE send_intents SET state = 'cancelled', state_reason = 'the mailbox was disconnected' WHERE tenant_id = :tenant_id "
        "AND mailbox_id = :id AND state = 'queued'",
        {"id": mailbox_id},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="mailbox.disconnected",
        target_type="mailbox",
        target_id=str(mailbox_id),
    )
    return _mailbox(row)


@public_router.post("/webhooks/gmail", status_code=204, include_in_schema=False)
def gmail_push(
    payload: dict[str, Any],
    auth: Annotated[GoogleAuth, Depends(get_google_auth)],
    store: Annotated[redis.Redis, Depends(get_redis)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Pub/Sub push. A notification only says "look"; the cursor is never taken from it."""
    if not auth.verify_push(authorization):
        raise UnauthenticatedError("The notification could not be verified.")
    message = payload.get("message") or {}
    message_id = str(message.get("messageId") or "")
    if message_id and not store.set(f"crm:gmail:push:{message_id}", "1", nx=True, ex=3600):
        return Response(status_code=204)  # delivered twice
    try:
        data = json.loads(base64.b64decode(message.get("data") or "").decode())
        address = normalize_email(str(data.get("emailAddress") or ""))
    except (ValueError, TypeError):
        return Response(status_code=204)
    if not address:
        return Response(status_code=204)
    # The tenant is found from the connection this installation recorded, never from the notification.
    with session_scope() as db:
        routes = db.execute(text("SELECT * FROM mailbox_route('gmail', :a)"), {"a": address}).all()
    for route in routes:
        with session_scope(RlsContext(tenant_id=route.tenant_id)) as db:
            db.execute(
                text("UPDATE mailboxes SET last_notification_at = now() WHERE tenant_id = :t AND id = :m"),
                {"t": route.tenant_id, "m": route.mailbox_id},
            )
            db.execute(
                text(
                    "UPDATE due_jobs SET due_at = now() WHERE tenant_id = :t AND kind = 'mailbox.sync' AND ref_id = :m AND status = 'pending'"
                ),
                {"t": route.tenant_id, "m": route.mailbox_id},
            )
    return Response(status_code=204)


# --- conversations ---------------------------------------------------------------------------

MESSAGE_COLUMNS = (
    "id, thread_id, direction, classification, from_address, to_addresses::text[] AS to_addresses, subject, snippet, body_text, body_html, "
    "has_remote_content, attachments, sent_at"
)


def _threads(
    ctx: TenantContext, condition: str, params: dict[str, Any], limit: int = 50, offset: int = 0
) -> list[ThreadOut]:
    threads = many(
        ctx,
        f"SELECT * FROM email_threads WHERE tenant_id = :tenant_id AND {condition} "
        "ORDER BY last_message_at DESC NULLS LAST, id LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    )
    if not threads:
        return []
    messages = many(
        ctx,
        f"SELECT {MESSAGE_COLUMNS} FROM email_messages WHERE tenant_id = :tenant_id AND thread_id = ANY(:ids) ORDER BY sent_at, id",
        {"ids": [t["id"] for t in threads]},
    )
    by_thread: dict[UUID, list[MessageOut]] = {}
    for message in messages:
        by_thread.setdefault(message["thread_id"], []).append(
            MessageOut(**{k: message[k] for k in MessageOut.model_fields})
        )
    return [
        ThreadOut(**{k: t[k] for k in ThreadOut.model_fields if k != "messages"}, messages=by_thread.get(t["id"], []))
        for t in threads
    ]


@router.get("/email-threads", response_model=Page[ThreadOut], operation_id="listEmailThreads", tags=["email"])
def list_threads(
    paging: Paging,
    ctx: TenantContext = READ,
    lead_id: UUID | None = None,
    company_id: UUID | None = None,
    needs_review: bool = False,
) -> Page[ThreadOut]:
    """Conversations for a record, or the review queue of replies that could not be matched to one."""
    condition = "true"
    typed: dict[str, Any] = {}
    if lead_id:
        condition, typed["l"] = "lead_id = :l", lead_id
    elif company_id:
        condition, typed["c"] = "company_id = :c", company_id
    elif needs_review:
        condition = "link_state IN ('unmatched', 'conflict') AND has_inbound"
    else:
        raise ValidationFailed("Say which record to list conversations for, or ask for the review queue.")
    total = scalar(ctx, f"SELECT count(*) FROM email_threads WHERE tenant_id = :tenant_id AND {condition}", typed)
    return Page(
        items=_threads(ctx, condition, typed, paging.limit, paging.offset),
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post(
    "/email-threads/{thread_id}/link", response_model=ThreadOut, operation_id="linkEmailThread", tags=["email"]
)
def link_thread(thread_id: UUID, body: LinkThread, ctx: TenantContext = tenant_with(Permission.CRM_WRITE)) -> ThreadOut:
    """A person decides which record an unmatched conversation belongs to, or that it belongs to none."""
    exists(ctx, "email_threads", thread_id, "The conversation")
    if body.ignore:
        execute(
            ctx,
            "UPDATE email_threads SET link_state = 'ignored', link_note = 'marked as not related by a user' WHERE tenant_id = :tenant_id AND id = :id",
            {"id": thread_id},
        )
    else:
        company_id = body.company_id
        if body.lead_id:
            company_id = scalar(
                ctx, "SELECT company_id FROM leads WHERE tenant_id = :tenant_id AND id = :l", {"l": body.lead_id}
            )
            if company_id is None:
                raise ValidationFailed("The lead was not found in this workspace.")
        if company_id is None:
            raise ValidationFailed("Choose a company or a lead.")
        exists(ctx, "companies", company_id, "The company")
        execute(
            ctx,
            "UPDATE email_threads SET company_id = :c, lead_id = :l, link_state = 'linked', link_note = 'linked by a user' "
            "WHERE tenant_id = :tenant_id AND id = :id",
            {"c": company_id, "l": body.lead_id, "id": thread_id},
        )
        log_activity(
            ctx, "email.linked", "Conversation linked to this record", company_id=company_id, lead_id=body.lead_id
        )
    return _threads(ctx, "id = :id", {"id": thread_id})[0]


# --- drafts ----------------------------------------------------------------------------------


def unsubscribe_url(tenant_id: UUID, address: str) -> str:
    from app.modules.email.unsubscribe import make_token

    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/unsubscribe/{make_token(tenant_id, address)}"


def rendered(ctx: TenantContext, draft: Any, mailbox: Any | None) -> tuple[str, eligibility.Decision]:
    """The final body and the eligibility decision for a draft as it stands now."""
    policy = eligibility.current_policy(ctx.db, ctx.tenant_id)
    identity = scalar(ctx, "SELECT sender_identity FROM tenants WHERE id = :tenant_id")
    foot = eligibility.footer(
        dict(policy["rules"]) if policy else {},
        kind=draft["kind"],
        sender_identity=identity,
        unsubscribe_url=unsubscribe_url(ctx.tenant_id, draft["to_address"]),
    )
    body = eligibility.render(draft["body_text"], foot)
    decision = eligibility.evaluate(
        ctx.db,
        ctx.tenant_id,
        to_address=draft["to_address"],
        kind=draft["kind"],
        subject=draft["subject"],
        rendered_body=body,
        sender_address=mailbox["email_address"] if mailbox and mailbox["status"] != "revoked" else None,
        company_id=draft["company_id"],
        thread_id=draft["thread_id"],
    )
    return body, decision


def _draft(ctx: TenantContext, draft_id: UUID) -> DraftOut:
    draft = one(
        ctx,
        "SELECT * FROM email_drafts WHERE tenant_id = :tenant_id AND id = :id",
        {"id": draft_id},
        "Draft not found.",
    )
    body, decision = rendered(ctx, draft, active_mailbox(ctx))
    return DraftOut(
        **{k: draft[k] for k in DraftOut.model_fields if k != "eligibility"},
        eligibility=EligibilityOut(
            outcome=decision.outcome,
            reasons=decision.reasons,
            checks=decision.checks,
            policy_version=decision.policy_version,
            rendered_body=body,
        ),
    )


@router.post("/email-drafts", response_model=DraftOut, status_code=201, operation_id="createEmailDraft", tags=["email"])
def create_draft(body: DraftIn, ctx: TenantContext = DRAFT) -> DraftOut:
    """Start a draft for a lead, or a reply in a conversation. Nothing is sent from here."""
    company_id, to_address, subject, text_body, thread_id = (
        None,
        body.to_address,
        body.subject,
        body.body_text,
        body.thread_id,
    )
    if body.kind == "reply":
        if thread_id is None:
            raise ValidationFailed("A reply needs the conversation it answers.")
        last = one(
            ctx,
            "SELECT m.from_address, m.subject, t.company_id, t.lead_id FROM email_messages m JOIN email_threads t ON t.tenant_id = m.tenant_id "
            "AND t.id = m.thread_id WHERE m.tenant_id = :tenant_id AND m.thread_id = :th AND m.direction = 'inbound' ORDER BY m.sent_at DESC LIMIT 1",
            {"th": thread_id},
            "There is no message from the other person to reply to.",
        )
        company_id = last["company_id"]
        to_address = to_address or last["from_address"]
        subject = subject or (
            last["subject"] if (last["subject"] or "").lower().startswith("re:") else f"Re: {last['subject'] or ''}"
        )
        lead_id = body.lead_id or last["lead_id"]
    else:
        if body.lead_id is None:
            raise ValidationFailed("Choose the lead this message is for.")
        lead = one(
            ctx,
            "SELECT l.company_id, a.outreach_opening, a.discovery_question FROM leads l LEFT JOIN lead_assessments a ON a.tenant_id = l.tenant_id "
            "AND a.lead_id = l.id AND a.is_current WHERE l.tenant_id = :tenant_id AND l.id = :l",
            {"l": body.lead_id},
            "Lead not found.",
        )
        company_id, lead_id = lead["company_id"], body.lead_id
        if to_address is None:
            # Only an address already recorded for the company is offered. None is ever guessed.
            to_address = scalar(
                ctx,
                "SELECT normalized_value FROM contact_channels WHERE tenant_id = :tenant_id AND company_id = :c AND kind = 'email' "
                "AND NOT do_not_contact AND verification_state <> 'invalid' ORDER BY position LIMIT 1",
                {"c": company_id},
            )
        text_body = (
            text_body or "\n\n".join(p for p in (lead["outreach_opening"], lead["discovery_question"]) if p) or None
        )
        subject = subject or scalar(
            ctx, "SELECT name FROM companies WHERE tenant_id = :tenant_id AND id = :c", {"c": company_id}
        )
    address = normalize_email(to_address or "")
    if address is None:
        raise ValidationFailed(
            "No email address is recorded for this company. Add one that the business has published or given you."
        )
    if not text_body or not subject:
        raise ValidationFailed("Write a subject and a message.")
    mailbox = active_mailbox(ctx)
    draft_id = scalar(
        ctx,
        "INSERT INTO email_drafts (tenant_id, mailbox_id, lead_id, company_id, thread_id, kind, to_address, subject, body_text, created_by) "
        "VALUES (:tenant_id, :m, :l, :c, :th, :k, :to, :s, :b, :u) RETURNING id",
        {
            "m": mailbox["id"] if mailbox else None,
            "l": lead_id,
            "c": company_id,
            "th": thread_id,
            "k": body.kind,
            "to": address,
            "s": subject[:300],
            "b": text_body,
            "u": ctx.user_id,
        },
    )
    return _draft(ctx, draft_id)


@router.get("/email-drafts", response_model=list[DraftOut], operation_id="listEmailDrafts", tags=["email"])
def list_drafts(ctx: TenantContext = READ, lead_id: UUID | None = None) -> list[DraftOut]:
    condition = "lead_id = :l" if lead_id else "status IN ('draft', 'approved', 'queued')"
    ids = many(
        ctx,
        f"SELECT id FROM email_drafts WHERE tenant_id = :tenant_id AND {condition} ORDER BY created_at DESC LIMIT 50",
        {"l": lead_id},
    )
    return [_draft(ctx, row["id"]) for row in ids]


@router.get("/email-drafts/{draft_id}", response_model=DraftOut, operation_id="getEmailDraft", tags=["email"])
def get_draft(draft_id: UUID, ctx: TenantContext = READ) -> DraftOut:
    return _draft(ctx, draft_id)


@router.patch("/email-drafts/{draft_id}", response_model=DraftOut, operation_id="updateEmailDraft", tags=["email"])
def update_draft(draft_id: UUID, body: DraftUpdate, ctx: TenantContext = DRAFT) -> DraftOut:
    """Any change to recipient, subject or text makes a new version and withdraws an earlier approval."""
    current = one(
        ctx,
        "SELECT * FROM email_drafts WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": draft_id},
        "Draft not found.",
    )
    if current["status"] in ("queued", "sent", "cancelled"):
        raise ConflictError("This message can no longer be edited.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "to_address" in changes:
        address = normalize_email(changes["to_address"])
        if address is None:
            raise ValidationFailed("Enter a valid email address.")
        changes["to_address"] = address
    changes = {k: v for k, v in changes.items() if v != current[k]}
    if changes:
        assignments = ", ".join(f"{k} = :{k}" for k in changes)
        execute(
            ctx,
            f"UPDATE email_drafts SET {assignments}, version = version + 1, status = 'draft', approved_version = NULL, "
            "approved_content_hash = NULL, approved_by = NULL, approved_at = NULL WHERE tenant_id = :tenant_id AND id = :id",
            {**changes, "id": draft_id},
        )
    return _draft(ctx, draft_id)


@router.delete("/email-drafts/{draft_id}", status_code=204, operation_id="discardEmailDraft", tags=["email"])
def discard_draft(draft_id: UUID, ctx: TenantContext = DRAFT) -> None:
    one(
        ctx,
        "UPDATE email_drafts SET status = 'cancelled' WHERE tenant_id = :tenant_id AND id = :id AND status IN ('draft', 'approved') RETURNING id",
        {"id": draft_id},
        "Draft not found or already sent.",
    )


# --- outreach rules --------------------------------------------------------------------------


def _policy(ctx: TenantContext) -> OutreachPolicyOut:
    policy = eligibility.current_policy(ctx.db, ctx.tenant_id)
    if policy is None:
        raise ConflictError("No outreach policy exists for this workspace.")
    identity = scalar(ctx, "SELECT sender_identity FROM tenants WHERE id = :tenant_id")
    source = many(
        ctx,
        "SELECT name, version, obtained_at, expires_at, entry_count, obtained_how FROM regulatory_sources WHERE tenant_id = :tenant_id "
        "AND jurisdiction = :j AND superseded_at IS NULL ORDER BY obtained_at DESC LIMIT 1",
        {"j": policy["jurisdiction"]},
    )
    register: dict[str, Any] = {"state": "unavailable", "note": "No opt-out register has been imported."}
    if source:
        s = dict(source[0])
        max_age = timedelta(days=int(policy["rules"].get("register_max_age_days") or 0))
        stale = s["expires_at"] <= utcnow() or (max_age and s["obtained_at"] < utcnow() - max_age)
        register = {
            "state": "stale" if stale else "current",
            **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in s.items()},
        }
    return OutreachPolicyOut(
        **{k: policy[k] for k in OutreachPolicyOut.model_fields if k in policy},
        sender_identity=identity,
        opt_out_register=register,
    )


@router.get(
    "/outreach-policy", response_model=OutreachPolicyOut, operation_id="getOutreachPolicy", tags=["outreach rules"]
)
def get_policy(ctx: TenantContext = READ) -> OutreachPolicyOut:
    return _policy(ctx)


@router.put(
    "/outreach-policy/sender-identity",
    response_model=OutreachPolicyOut,
    operation_id="setSenderIdentity",
    tags=["outreach rules"],
)
def set_sender_identity(body: SenderIdentity, ctx: TenantContext = SETTINGS) -> OutreachPolicyOut:
    execute(ctx, "UPDATE tenants SET sender_identity = :s WHERE id = :tenant_id", {"s": body.sender_identity.strip()})
    return _policy(ctx)


@router.post(
    "/outreach-policy/approve",
    response_model=OutreachPolicyOut,
    operation_id="approveOutreachPolicy",
    tags=["outreach rules"],
)
def approve_policy(
    body: PolicyApprove, ctx: TenantContext = tenant_with(Permission.OWNERS_MANAGE)
) -> OutreachPolicyOut:
    """An owner records that the rules were checked against current requirements. Until then unsolicited email is blocked."""
    current = eligibility.current_policy(ctx.db, ctx.tenant_id)
    if current is None:
        raise ConflictError("No outreach policy exists for this workspace.")
    if not scalar(ctx, "SELECT sender_identity FROM tenants WHERE id = :tenant_id"):
        raise ValidationFailed("Set the sender identity before approving the policy.")
    rules = {
        **current["rules"],
        "register_max_age_days": body.register_max_age_days,
        "label_text": body.label_text,
        "sole_trader": body.sole_trader,
        "requires_register_check": body.requires_register_check,
    }
    version = f"{current['jurisdiction'].lower()}-{body.source_checked_on.isoformat()}-{hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:8]}"
    execute(
        ctx,
        "UPDATE outreach_policies SET status = 'retired' WHERE tenant_id = :tenant_id AND jurisdiction = :j AND status = 'approved'",
        {"j": current["jurisdiction"]},
    )
    execute(
        ctx,
        "INSERT INTO outreach_policies (tenant_id, jurisdiction, version, status, rules, source_checked_on, source_note, approved_by, approved_at, "
        "approval_note) VALUES (:tenant_id, :j, :v, 'approved', CAST(:r AS jsonb), :d, :sn, :u, now(), :n) "
        "ON CONFLICT (tenant_id, jurisdiction, version) DO UPDATE SET status = 'approved', approved_by = EXCLUDED.approved_by, "
        "approved_at = now(), approval_note = EXCLUDED.approval_note",
        {
            "j": current["jurisdiction"],
            "v": version,
            "r": json.dumps(rules),
            "d": body.source_checked_on,
            "sn": current["source_note"],
            "u": ctx.user_id,
            "n": body.approval_note,
        },
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="outreach_policy.approved",
        target_type="outreach_policy",
        target_id=version,
        data={"note": body.approval_note, "rules": rules},
    )
    return _policy(ctx)


@router.post("/regulatory-sources", operation_id="importRegulatorySource", status_code=201, tags=["outreach rules"])
def import_regulatory_source(
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form(min_length=3, max_length=200)],
    version: Annotated[str, Form(min_length=1, max_length=100)],
    obtained_how: Annotated[str, Form(min_length=10, max_length=500)],
    valid_days: Annotated[int, Form(ge=1, le=90)],
    ctx: TenantContext = SETTINGS,
    jurisdiction: Annotated[str, Form(pattern=r"^[A-Z]{2}$")] = "BG",
) -> dict[str, Any]:
    """Import an opt-out register that was obtained through its official channel: one address per line.

    The application does not fetch a register by itself, and never treats a missing one as empty.
    """
    data = file.file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValidationFailed("The register file is limited to 20 MB.")
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValidationFailed("The register file must be UTF-8 text with one address per line.") from exc
    addresses = sorted({a for a in (normalize_email(line) for line in lines if line.strip()) if a})
    rejected = sum(1 for line in lines if line.strip() and normalize_email(line) is None)
    execute(
        ctx,
        "UPDATE regulatory_sources SET superseded_at = now() WHERE tenant_id = :tenant_id AND jurisdiction = :j AND superseded_at IS NULL",
        {"j": jurisdiction},
    )
    source_id = scalar(
        ctx,
        "INSERT INTO regulatory_sources (tenant_id, jurisdiction, name, version, sha256, obtained_at, expires_at, entry_count, obtained_how, imported_by) "
        "VALUES (:tenant_id, :j, :n, :v, :h, now(), now() + make_interval(days => :days), :c, :how, :u) RETURNING id",
        {
            "j": jurisdiction,
            "n": name,
            "v": version,
            "h": hashlib.sha256(data).digest(),
            "days": valid_days,
            "c": len(addresses),
            "how": obtained_how,
            "u": ctx.user_id,
        },
    )
    if addresses:
        ctx.db.execute(
            text("INSERT INTO regulatory_entries (tenant_id, source_id, address) VALUES (:t, :s, :a)"),
            [{"t": ctx.tenant_id, "s": source_id, "a": a} for a in addresses],
        )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="regulatory_source.imported",
        target_type="regulatory_source",
        target_id=str(source_id),
        data={
            "name": name,
            "version": version,
            "entries": len(addresses),
            "rejected_lines": rejected,
            "sha256": hashlib.sha256(data).hexdigest(),
            "obtained_how": obtained_how,
        },
    )
    return {"id": str(source_id), "entries": len(addresses), "rejected_lines": rejected}


@router.get(
    "/email-suppressions",
    response_model=Page[SuppressionOut],
    operation_id="listEmailSuppressions",
    tags=["outreach rules"],
)
def list_suppressions(paging: Paging, ctx: TenantContext = READ) -> Page[SuppressionOut]:
    total = scalar(ctx, "SELECT count(*) FROM email_suppressions WHERE tenant_id = :tenant_id")
    rows = many(
        ctx,
        "SELECT * FROM email_suppressions WHERE tenant_id = :tenant_id ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset",
        {"limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[SuppressionOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


def suppress(
    db: Any,
    tenant_id: UUID,
    *,
    scope: str,
    value: str,
    reason: str,
    source: str,
    note: str | None,
    created_by: UUID | None,
) -> None:
    db.execute(
        text(
            "INSERT INTO email_suppressions (tenant_id, scope, value, reason, source, note, created_by) VALUES (:t, :sc, :v, :r, :src, :n, :u) "
            "ON CONFLICT DO NOTHING"
        ),
        {"t": tenant_id, "sc": scope, "v": value, "r": reason, "src": source, "n": note, "u": created_by},
    )
    # Anything still waiting to go to this recipient is stopped now, not at dispatch time.
    condition = "to_address = :v" if scope == "address" else "split_part(to_address::text, '@', 2) = :v"
    db.execute(
        text(
            f"UPDATE send_intents SET state = 'cancelled', state_reason = 'the recipient was suppressed' WHERE tenant_id = :t AND state = 'queued' AND {condition}"
        ),
        {"t": tenant_id, "v": value},
    )


@router.post(
    "/email-suppressions",
    response_model=SuppressionOut,
    status_code=201,
    operation_id="createEmailSuppression",
    tags=["outreach rules"],
)
def create_suppression(body: SuppressionIn, ctx: TenantContext = DRAFT) -> SuppressionOut:
    value = normalize_email(body.value) if body.scope == "address" else body.value.lower().lstrip("@")
    if not value or (body.scope == "domain" and "." not in value):
        raise ValidationFailed("Enter a valid email address or domain.")
    suppress(
        ctx.db,
        ctx.tenant_id,
        scope=body.scope,
        value=value,
        reason=body.reason,
        source="manual",
        note=body.note,
        created_by=ctx.user_id,
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="email.suppressed",
        target_type="email_suppression",
        data={"scope": body.scope, "reason": body.reason},
    )
    return SuppressionOut(
        **one(
            ctx,
            "SELECT * FROM email_suppressions WHERE tenant_id = :tenant_id AND scope = :s AND value = :v AND lifted_at IS NULL",
            {"s": body.scope, "v": value},
            "Suppression not found.",
        )
    )


@router.post(
    "/email-suppressions/{suppression_id}/lift",
    response_model=SuppressionOut,
    operation_id="liftEmailSuppression",
    tags=["outreach rules"],
)
def lift_suppression(
    suppression_id: UUID, body: LiftIn, ctx: TenantContext = tenant_with(Permission.OUTREACH_APPROVE)
) -> SuppressionOut:
    row = one(
        ctx,
        "UPDATE email_suppressions SET lifted_at = now(), lifted_by = :u, lift_note = :n WHERE tenant_id = :tenant_id AND id = :id "
        "AND lifted_at IS NULL RETURNING *",
        {"u": ctx.user_id, "n": body.lift_note, "id": suppression_id},
        "Suppression not found or already lifted.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="email.suppression_lifted",
        target_type="email_suppression",
        target_id=str(suppression_id),
        data={"note": body.lift_note, "reason": row["reason"]},
    )
    return SuppressionOut(**row)


@router.put("/recipient-profiles", operation_id="setRecipientProfile", tags=["outreach rules"])
def set_recipient_profile(
    body: ProfileIn, ctx: TenantContext = tenant_with(Permission.RESEARCH_REVIEW)
) -> dict[str, Any]:
    """Record what kind of recipient an address belongs to, and the evidence. The mail domain is not evidence."""
    address = normalize_email(body.address)
    if address is None:
        raise ValidationFailed("Enter a valid email address.")
    classified = body.legal_form != "unknown" or body.context != "unknown"
    if classified and not (body.evidence or "").strip():
        raise ValidationFailed("Say what this classification is based on, for example the company register entry.")
    row = one(
        ctx,
        "INSERT INTO recipient_profiles (tenant_id, address, legal_form, context, evidence, evidence_url, classified_by, classified_at) "
        "VALUES (:tenant_id, :a, :lf, :c, :e, :url, :u, now()) ON CONFLICT (tenant_id, address) DO UPDATE SET legal_form = EXCLUDED.legal_form, "
        "context = EXCLUDED.context, evidence = EXCLUDED.evidence, evidence_url = EXCLUDED.evidence_url, classified_by = EXCLUDED.classified_by, "
        "classified_at = now() RETURNING address, legal_form, context, evidence, evidence_url, classified_at",
        {
            "a": address,
            "lf": body.legal_form,
            "c": body.context,
            "e": body.evidence if classified else None,
            "url": body.evidence_url,
            "u": ctx.user_id if classified else None,
        },
        "Profile not found.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="recipient.classified",
        target_type="recipient_profile",
        data={"legal_form": body.legal_form, "context": body.context},
    )
    return dict(row)


@router.post("/email-consents", status_code=201, operation_id="recordEmailConsent", tags=["outreach rules"])
def record_consent(body: ConsentIn, ctx: TenantContext = DRAFT) -> dict[str, Any]:
    """Record a request or consent with its scope. A request for one follow-up is not consent to campaigns."""
    address = normalize_email(body.address)
    if address is None:
        raise ValidationFailed("Enter a valid email address.")
    row = one(
        ctx,
        "INSERT INTO email_consents (tenant_id, address, scope, kind, source, evidence, obtained_at, recorded_by) "
        "VALUES (:tenant_id, :a, :scope, :kind, :source, :evidence, :obtained_at, :u) RETURNING id, address, scope, kind, source, obtained_at",
        {**body.model_dump(exclude={"address"}), "a": address, "u": ctx.user_id},
        "Consent not found.",
    )
    return {**dict(row), "id": str(row["id"])}
