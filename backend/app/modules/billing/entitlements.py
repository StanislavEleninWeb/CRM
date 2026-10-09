"""What a workspace may do right now.

One function answers that for the API, for API keys and for background workers. It reads
the subscription state this application last confirmed with the billing provider; nothing
a browser or a redirect says can change it.

Policy when a workspace is restricted (trial over, payment overdue beyond the grace period,
subscription ended, or a price this application does not recognise):

* Everything stays readable and exportable, and billing stays reachable. No data is deleted.
* Nothing new is created or sent. A research run in progress pauses before its next paid
  step; what it already spent is settled. An email that has not started sending is stopped;
  one already handed to the mailbox provider is not affected.
* Paying again lifts the restriction; paused runs can be resumed.

Limits (seats, research runs and emails per calendar month) apply while the workspace is in
good standing. Going over a limit never charges anything: the action is refused.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.time import utcnow

Standing = Literal["good", "grace", "restricted"]
UNLIMITED = 1_000_000
PILOT: dict[str, Any] = {
    "code": "internal_pilot",
    "name": "Internal pilot",
    "is_test": False,
    "price_note": "Billing is switched off on this installation.",
    "seats_included": UNLIMITED,
    "research_runs_per_month": UNLIMITED,
    "emails_per_month": UNLIMITED,
    "api_keys": UNLIMITED,
    "webhook_endpoints": UNLIMITED,
    "platform_ai_allowance": 0,
    "allow_paid_overage": False,
}


class PaymentRequiredError(AppError):
    status_code = 402
    code = "subscription_required"


class LimitReachedError(AppError):
    status_code = 409
    code = "plan_limit_reached"


@dataclass(frozen=True)
class Entitlement:
    standing: Standing
    reason: str | None
    plan: dict[str, Any]
    status: str
    seats: int
    trial_ends_at: datetime | None = None
    grace_until: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    billing_enforced: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def restricted(self) -> bool:
        return self.standing == "restricted"

    def limit(self, name: str) -> int:
        return int(self.plan[name])


def ensure_row(db: Session, tenant_id: UUID) -> Any:
    """Every workspace starts on a trial, dated from when it was created."""
    row = (
        db.execute(text("SELECT * FROM tenant_billing WHERE tenant_id = :t"), {"t": tenant_id}).mappings().one_or_none()
    )
    if row is not None:
        return row
    db.execute(
        text(
            "INSERT INTO tenant_billing (tenant_id, plan_code, status, seats, trial_ends_at) "
            "SELECT t.id, 'trial', 'trialing', p.seats_included, t.created_at + make_interval(days => :days) "
            "FROM tenants t, plans p WHERE t.id = :t AND p.code = 'trial' ON CONFLICT (tenant_id) DO NOTHING"
        ),
        {"t": tenant_id, "days": get_settings().trial_days},
    )
    return db.execute(text("SELECT * FROM tenant_billing WHERE tenant_id = :t"), {"t": tenant_id}).mappings().one()


def evaluate(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> Entitlement:
    settings = get_settings()
    if settings.billing_mode == "off":
        return Entitlement("good", None, PILOT, "not_billed", UNLIMITED, billing_enforced=False)
    now = now or utcnow()
    row = ensure_row(db, tenant_id)
    plan = dict(db.execute(text("SELECT * FROM plans WHERE code = :c"), {"c": row["plan_code"]}).mappings().one())
    common = {
        "plan": plan,
        "status": row["status"],
        "seats": row["seats"],
        "trial_ends_at": row["trial_ends_at"],
        "current_period_end": row["current_period_end"],
        "cancel_at_period_end": row["cancel_at_period_end"],
    }
    status = row["status"]
    if status == "trialing":
        if row["trial_ends_at"] and row["trial_ends_at"] <= now:
            return Entitlement("restricted", "The trial has ended. Choose a plan to continue.", **common)
        return Entitlement("good", None, **common)
    if status == "active":
        notes = []
        if row["cancel_at_period_end"] and row["current_period_end"]:
            notes.append("The subscription ends at the end of the paid period and will not renew.")
        return Entitlement("good", None, notes=notes, **common)
    if status == "past_due":
        since = row["past_due_since"] or now
        grace_until = since + timedelta(days=settings.past_due_grace_days)
        if now < grace_until:
            return Entitlement(
                "grace",
                "A payment is overdue. Update the payment method to avoid interruption.",
                grace_until=grace_until,
                **common,
            )
        return Entitlement(
            "restricted",
            "A payment has been overdue for too long. Update the payment method.",
            grace_until=grace_until,
            **common,
        )
    reasons = {
        "canceled": "The subscription has ended. Choose a plan to continue.",
        "unpaid": "The subscription is unpaid. Update the payment method.",
        "incomplete": "The first payment was not completed. Finish checkout or choose a plan.",
        "unrecognized": "The subscription is for a price this application does not recognise. Contact support.",
    }
    return Entitlement("restricted", reasons.get(status, "The subscription is not active."), **common)


def require_active(db: Session, tenant_id: UUID, action: str) -> Entitlement:
    entitlement = evaluate(db, tenant_id)
    if entitlement.restricted:
        raise PaymentRequiredError(f"{action} is not available: {entitlement.reason}")
    return entitlement


def _month_start(db: Session, tenant_id: UUID) -> datetime:
    zone = ZoneInfo(db.execute(text("SELECT timezone FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar_one())
    local = utcnow().astimezone(zone)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage(db: Session, tenant_id: UUID) -> dict[str, int]:
    since = _month_start(db, tenant_id)
    scope = {"t": tenant_id, "since": since}
    return {
        "seats": int(
            db.execute(
                text(
                    "SELECT (SELECT count(*) FROM memberships WHERE tenant_id = :t) + "
                    "(SELECT count(*) FROM invitations WHERE tenant_id = :t AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now())"
                ),
                scope,
            ).scalar_one()
        ),
        "research_runs_this_month": int(
            db.execute(
                text("SELECT count(*) FROM research_runs WHERE tenant_id = :t AND created_at >= :since"), scope
            ).scalar_one()
        ),
        "emails_this_month": int(
            db.execute(
                text(
                    "SELECT count(*) FROM send_intents WHERE tenant_id = :t AND created_at >= :since AND NOT dry_run "
                    "AND state NOT IN ('cancelled', 'blocked', 'failed')"
                ),
                scope,
            ).scalar_one()
        ),
        "api_keys": int(
            db.execute(
                text(
                    "SELECT count(*) FROM api_keys WHERE tenant_id = :t AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"
                ),
                scope,
            ).scalar_one()
        ),
        "webhook_endpoints": int(
            db.execute(text("SELECT count(*) FROM webhook_endpoints WHERE tenant_id = :t"), scope).scalar_one()
        ),
    }


LIMITS = {
    "seats": ("seats", "members and open invitations"),
    "research_runs_this_month": ("research_runs_per_month", "research runs this month"),
    "emails_this_month": ("emails_per_month", "emails this month"),
    "api_keys": ("api_keys", "API keys"),
    "webhook_endpoints": ("webhook_endpoints", "webhook addresses"),
}


def allowance(entitlement: Entitlement, counter: str) -> int:
    if counter == "seats":
        return max(entitlement.seats, 1) if entitlement.billing_enforced else UNLIMITED
    return entitlement.limit(LIMITS[counter][0])


def require_room(db: Session, tenant_id: UUID, counter: str, action: str) -> Entitlement:
    """Refuse an action that would go over a plan limit. Serialised per tenant, so two requests cannot both take the last place."""
    entitlement = require_active(db, tenant_id, action)
    if not entitlement.billing_enforced:
        return entitlement
    db.execute(text("SELECT 1 FROM tenant_billing WHERE tenant_id = :t FOR UPDATE"), {"t": tenant_id})
    used, allowed = usage(db, tenant_id)[counter], allowance(entitlement, counter)
    if used >= allowed:
        raise LimitReachedError(
            f"{action} is not possible: the plan allows {allowed} {LIMITS[counter][1]} and {used} are in use. Nothing extra is charged."
        )
    return entitlement
