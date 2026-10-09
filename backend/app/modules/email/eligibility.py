"""Whether an email may be sent to a recipient.

One function decides, for every path that can send: preview, request and the final
check immediately before dispatch. It never guesses. A missing fact is a reason to send
the message to review, and a check that cannot be completed (a stale or unavailable
register) blocks unsolicited sending rather than being treated as a pass.

Three different things are kept apart on purpose:
  * consent or an explicit request from the recipient,
  * the lawful basis for holding their data (not decided here),
  * permission to use a channel (suppressions and restrictions).

This module encodes a configurable policy. It is not legal advice, and the Bulgarian
policy stays in draft, blocking unsolicited email, until a named person approves it.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.normalize import normalize_email
from app.core.time import utcnow

Kind = Literal["unsolicited", "reply", "requested"]
Stage = Literal["preview", "request", "dispatch"]
UNSUBSCRIBE_MARK = "Отписване / Unsubscribe:"


@dataclass
class Decision:
    outcome: Literal["allow", "review", "block"]
    reasons: list[dict[str, str]]
    checks: dict[str, Any]
    policy_version: str | None
    content_hash: str

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


@dataclass
class _Collector:
    reasons: list[dict[str, str]] = field(default_factory=list)

    def add(self, level: str, code: str, message: str) -> None:
        self.reasons.append({"level": level, "code": code, "message": message})

    @property
    def outcome(self) -> Literal["allow", "review", "block"]:
        levels = {r["level"] for r in self.reasons}
        return "block" if "block" in levels else "review" if "review" in levels else "allow"


def content_hash(to_address: str, subject: str, body: str, sender: str) -> str:
    """Binds an approval to exactly this recipient, sender and content."""
    return hashlib.sha256(
        json.dumps([to_address.lower(), subject, body, sender.lower()], ensure_ascii=False).encode()
    ).hexdigest()


def lock_recipient(db: Session, tenant_id: UUID, address: str) -> None:
    """Serialise decisions about one recipient until the transaction ends.

    The final check before a send and anything that would stop it (an opt-out, a suppression,
    a reply, a permanent bounce) take this lock, so one of them is always seen by the other.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"email-recipient:{tenant_id}:{address.lower()}"},
    )


def current_policy(db: Session, tenant_id: UUID, jurisdiction: str = "BG") -> Any:
    """The approved policy if there is one, otherwise the latest draft."""
    return (
        db.execute(
            text(
                "SELECT * FROM outreach_policies WHERE tenant_id = :t AND jurisdiction = :j AND status IN ('approved', 'draft') "
                "ORDER BY (status = 'approved') DESC, created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "j": jurisdiction},
        )
        .mappings()
        .one_or_none()
    )


def footer(rules: dict[str, Any], *, kind: Kind, sender_identity: str | None, unsubscribe_url: str | None) -> str:
    """The part of a message the sender cannot edit away: identification, identity and opt-out."""
    lines: list[str] = []
    if kind == "unsolicited" and rules.get("requires_unsolicited_label") and rules.get("label_text"):
        lines.append(str(rules["label_text"]))
    if sender_identity:
        lines.append(sender_identity)
    if kind != "reply" and unsubscribe_url:
        lines.append(f"{UNSUBSCRIBE_MARK} {unsubscribe_url}")
    return "\n".join(lines)


def render(body: str, footer_text: str) -> str:
    return f"{body.rstrip()}\n\n--\n{footer_text}" if footer_text else body.rstrip()


def register_check(
    db: Session, tenant_id: UUID, address: str, rules: dict[str, Any], jurisdiction: str = "BG"
) -> dict[str, Any]:
    """Look the address up in the imported opt-out register.

    ``match`` and ``clear`` are only returned from a current list. An old list is ``stale``
    and a missing one is ``unavailable``: neither is ever reported as clear.
    """
    if not rules.get("requires_register_check"):
        return {"result": "not_applicable"}
    source = (
        db.execute(
            text(
                "SELECT * FROM regulatory_sources WHERE tenant_id = :t AND jurisdiction = :j AND superseded_at IS NULL "
                "ORDER BY obtained_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "j": jurisdiction},
        )
        .mappings()
        .one_or_none()
    )
    if source is None:
        return {"result": "unavailable"}
    info = {"source": source["name"], "version": source["version"], "obtained_at": source["obtained_at"].isoformat()}
    max_age = timedelta(days=int(rules.get("register_max_age_days") or 0))
    if source["expires_at"] <= utcnow() or (max_age and source["obtained_at"] < utcnow() - max_age):
        return {"result": "stale", **info}
    found = db.execute(
        text("SELECT 1 FROM regulatory_entries WHERE tenant_id = :t AND source_id = :s AND address = :a LIMIT 1"),
        {"t": tenant_id, "s": source["id"], "a": address},
    ).first()
    return {"result": "match" if found else "clear", **info}


def evaluate(
    db: Session,
    tenant_id: UUID,
    *,
    to_address: str,
    kind: Kind,
    subject: str,
    rendered_body: str,
    sender_address: str | None,
    company_id: UUID | None = None,
    thread_id: UUID | None = None,
    stage: Stage = "preview",
) -> Decision:
    out = _Collector()
    checks: dict[str, Any] = {}
    address = normalize_email(to_address)
    digest = content_hash(to_address, subject, rendered_body, sender_address or "")
    if address is None:
        out.add("block", "invalid_address", "The recipient address is not a valid email address.")
        return Decision("block", out.reasons, checks, None, digest)
    domain = address.split("@", 1)[1]
    scope = {"t": tenant_id, "a": address, "d": domain}

    # --- channel permission: always applies ---
    suppression = db.execute(
        text(
            "SELECT reason, scope FROM email_suppressions WHERE tenant_id = :t AND lifted_at IS NULL "
            "AND ((scope = 'address' AND value = :a) OR (scope = 'domain' AND value = :d)) LIMIT 1"
        ),
        scope,
    ).one_or_none()
    checks["local_suppression"] = suppression.reason if suppression else "clear"
    replying_to_inbound = False
    if kind == "reply" and thread_id is not None:
        replying_to_inbound = bool(
            db.execute(
                text(
                    "SELECT 1 FROM email_messages WHERE tenant_id = :t AND thread_id = :th AND direction = 'inbound' "
                    "AND from_address = :a AND classification IN ('message', 'reply') LIMIT 1"
                ),
                {"t": tenant_id, "th": thread_id, "a": address},
            ).first()
        )
    checks["replying_to_their_message"] = replying_to_inbound
    if suppression and not (replying_to_inbound and suppression.reason not in ("permanent_bounce", "complaint")):
        out.add("block", "suppressed", f"This recipient is suppressed ({suppression.reason.replace('_', ' ')}).")
    if company_id is not None:
        restricted = db.execute(
            text(
                "SELECT reason FROM contact_restrictions WHERE tenant_id = :t AND company_id = :c AND lifted_at IS NULL "
                "AND channel_kind IN ('email', 'any') LIMIT 1"
            ),
            {"t": tenant_id, "c": company_id},
        ).scalar_one_or_none()
        flagged = db.execute(
            text(
                "SELECT restriction_reason FROM contact_channels WHERE tenant_id = :t AND company_id = :c AND kind = 'email' "
                "AND normalized_value = :a AND do_not_contact LIMIT 1"
            ),
            {"t": tenant_id, "c": company_id, "a": address},
        ).scalar_one_or_none()
        checks["do_not_contact"] = bool(restricted or flagged)
        if (restricted or flagged) and not replying_to_inbound:
            out.add("block", "do_not_contact", f"Do not email: {restricted or flagged}.")
    if not sender_address:
        out.add("block", "no_mailbox", "No mailbox is connected to send from.")

    policy = current_policy(db, tenant_id)
    rules: dict[str, Any] = dict(policy["rules"]) if policy else {}
    version = policy["version"] if policy else None
    checks["policy"] = {"version": version, "status": policy["status"] if policy else "missing"}

    if kind == "reply":
        if not replying_to_inbound:
            out.add("block", "not_a_reply", "A reply needs a message from this person in the same conversation.")
    elif kind == "requested":
        consent = (
            db.execute(
                text(
                    "SELECT scope, kind, obtained_at FROM email_consents WHERE tenant_id = :t AND address = :a AND revoked_at IS NULL "
                    "ORDER BY obtained_at DESC LIMIT 1"
                ),
                scope,
            )
            .mappings()
            .one_or_none()
        )
        checks["request_on_record"] = (
            dict(consent) | {"obtained_at": consent["obtained_at"].isoformat()} if consent else None
        )
        if consent is None:
            out.add("block", "no_request_on_record", "There is no recorded request or consent from this recipient.")
        if rules.get("requires_sender_identity") and not _identity_present(db, tenant_id, rendered_body):
            out.add("block", "sender_identity_missing", "The message does not identify the sender.")
    else:
        _unsolicited_rules(db, tenant_id, out, checks, policy, rules, address, rendered_body)

    return Decision(out.outcome, out.reasons, checks, version, digest)


def _identity_present(db: Session, tenant_id: UUID, rendered_body: str) -> bool:
    identity = db.execute(
        text("SELECT sender_identity FROM tenants WHERE id = :t"), {"t": tenant_id}
    ).scalar_one_or_none()
    return bool(identity and identity.strip() and identity.strip() in rendered_body)


def _unsolicited_rules(
    db: Session,
    tenant_id: UUID,
    out: _Collector,
    checks: dict[str, Any],
    policy: Any,
    rules: dict[str, Any],
    address: str,
    rendered_body: str,
) -> None:
    if policy is None or policy["status"] != "approved":
        out.add(
            "block",
            "policy_not_approved",
            "Unsolicited email is blocked until the outreach policy for this market is approved by an owner.",
        )
    profile = (
        db.execute(
            text("SELECT legal_form, context, evidence FROM recipient_profiles WHERE tenant_id = :t AND address = :a"),
            {"t": tenant_id, "a": address},
        )
        .mappings()
        .one_or_none()
    )
    legal_form = profile["legal_form"] if profile else "unknown"
    context = profile["context"] if profile else "unknown"
    checks["recipient"] = {
        "legal_form": legal_form,
        "context": context,
        "evidence": profile["evidence"] if profile else None,
    }
    has_consent = bool(
        db.execute(
            text(
                "SELECT 1 FROM email_consents WHERE tenant_id = :t AND address = :a AND revoked_at IS NULL AND kind = 'opt_in' LIMIT 1"
            ),
            {"t": tenant_id, "a": address},
        ).first()
    )
    if (legal_form == "natural_person" or context == "consumer") and not has_consent:
        out.add(
            "block",
            "consent_required",
            "This recipient is a private person or consumer and has not consented to commercial email.",
        )
    elif legal_form == "unknown" or context == "unknown":
        # The mailbox domain says nothing about legal form: a free-mail address can belong to a company.
        out.add(
            "review",
            "recipient_unclassified",
            "Record whether this address belongs to a company, a sole trader or a private person, with evidence.",
        )
    elif legal_form == "sole_trader" and rules.get("sole_trader") == "review":
        out.add("review", "sole_trader_review", "The policy requires a reviewed decision for sole traders.")

    register = register_check(db, tenant_id, address, rules)
    checks["register"] = register
    if register["result"] == "match":
        out.add("block", "on_opt_out_register", "This address is on the opt-out register.")
    elif register["result"] == "stale":
        out.add(
            "block",
            "register_stale",
            "The opt-out register on file is out of date. Import a current one before sending.",
        )
    elif register["result"] == "unavailable":
        out.add(
            "block",
            "register_unavailable",
            "No opt-out register has been imported, so the required check cannot be made.",
        )

    label = str(rules.get("label_text") or "")
    if rules.get("requires_unsolicited_label"):
        present = bool(label) and label in rendered_body
        checks["unsolicited_label"] = present
        if not present:
            out.add(
                "block", "label_missing", "The message is not identified as an unsolicited commercial communication."
            )
    if rules.get("requires_sender_identity"):
        present = _identity_present(db, tenant_id, rendered_body)
        checks["sender_identity"] = present
        if not present:
            out.add(
                "block",
                "sender_identity_missing",
                "The message does not identify the sender. Set the sender identity in outreach settings.",
            )
    if UNSUBSCRIBE_MARK not in rendered_body:
        out.add("block", "no_opt_out", "The message has no way to opt out.")


def record(
    db: Session,
    tenant_id: UUID,
    decision: Decision,
    *,
    to_address: str,
    kind: str,
    stage: Stage,
    draft_id: UUID | None,
    send_intent_id: UUID | None = None,
    decided_by: UUID | None = None,
) -> None:
    db.execute(
        text(
            "INSERT INTO eligibility_decisions (tenant_id, draft_id, send_intent_id, to_address, kind, stage, outcome, reasons, checks, "
            "policy_version, content_hash, decided_by) VALUES (:t, :d, :s, :a, :k, :stage, :o, CAST(:r AS jsonb), CAST(:c AS jsonb), :pv, :h, :u)"
        ),
        {
            "t": tenant_id,
            "d": draft_id,
            "s": send_intent_id,
            "a": to_address,
            "k": kind,
            "stage": stage,
            "o": decision.outcome,
            "r": json.dumps(decision.reasons),
            "c": json.dumps(decision.checks, default=str),
            "pv": decision.policy_version,
            "h": decision.content_hash,
            "u": decided_by,
        },
    )
