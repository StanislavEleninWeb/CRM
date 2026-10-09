"""Keeping the stored subscription state equal to what the billing provider says.

Events from the provider are treated as a signal that something changed, never as the new
state: after any event the customer's subscriptions are read from the provider and applied.
That is why duplicate and out-of-order events converge, and why a forged or replayed payload
cannot grant anything.
"""

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger
from app.core.time import utcnow
from app.modules.billing import entitlements
from app.modules.billing.stripe import BillingError, BillingProvider, StripeProvider, Subscription, verify_signature

log = get_logger(__name__)
DUE_KIND = "billing.reconcile"
STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "unpaid": "unpaid",
    "paused": "unpaid",
    "incomplete": "incomplete",
    "incomplete_expired": "canceled",
}
_provider: BillingProvider | None = None


def set_provider(provider: BillingProvider | None) -> None:
    global _provider
    _provider = provider


def get_provider() -> BillingProvider:
    if _provider is not None:
        return _provider
    key = get_settings().stripe_secret_key
    if not key:
        raise BillingError("Billing is not configured on this installation.")
    return StripeProvider(key)


def customer_id(db: Session, tenant_id: UUID) -> str | None:
    return db.execute(
        text("SELECT provider_customer_id FROM billing_customers WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar_one_or_none()


def ensure_customer(db: Session, tenant_id: UUID) -> str:
    from app.worker.due import schedule

    existing = customer_id(db, tenant_id)
    if existing:
        return existing
    name = db.execute(text("SELECT name FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar_one()
    created = get_provider().create_customer(name=name, tenant_id=str(tenant_id))
    db.execute(
        text(
            "INSERT INTO billing_customers (tenant_id, provider_customer_id) VALUES (:t, :c) ON CONFLICT (tenant_id) DO NOTHING"
        ),
        {"t": tenant_id, "c": created},
    )
    # A daily check, so a lost event cannot leave the state wrong for long.
    schedule(db, tenant_id, kind=DUE_KIND, unique_key="daily", due_at=utcnow() + timedelta(hours=24))
    return customer_id(db, tenant_id) or created


def _choose(subscriptions: list[Subscription]) -> Subscription | None:
    """The subscription that currently matters: a live one if there is one, else the most recently ended."""
    live = [s for s in subscriptions if s.status not in ("canceled", "incomplete_expired")]
    pool = live or subscriptions
    return max(
        pool, key=lambda s: (s.current_period_end or datetime.min.replace(tzinfo=utcnow().tzinfo), s.id), default=None
    )


def apply(db: Session, tenant_id: UUID, subscription: Subscription | None) -> str:
    """Store what the provider says. Returns a short description of what was done."""
    current = entitlements.ensure_row(db, tenant_id)
    if subscription is None:
        db.execute(
            text("UPDATE tenant_billing SET synced_at = now(), sync_error = NULL WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        return "no subscription"
    if subscription.metadata.get("tenant_id") not in (None, "", str(tenant_id)):
        # A subscription made for another workspace is attached to this customer. It grants nothing here.
        db.execute(
            text("UPDATE tenant_billing SET synced_at = now(), sync_error = :e WHERE tenant_id = :t"),
            {"t": tenant_id, "e": "A subscription belonging to another workspace was ignored."},
        )
        return "ignored: belongs to another workspace"
    plan = db.execute(
        text("SELECT code FROM plans WHERE provider_price_id = :p"), {"p": subscription.price_id}
    ).scalar_one_or_none()
    status = STATUS_MAP.get(subscription.status, "unrecognized") if plan else "unrecognized"
    past_due_since = (current["past_due_since"] or utcnow()) if status == "past_due" else None
    db.execute(
        text(
            "UPDATE tenant_billing SET plan_code = COALESCE(:plan, plan_code), status = :s, seats = :q, current_period_end = :end, "
            "cancel_at_period_end = :cape, canceled_at = :ca, past_due_since = :pds, provider_subscription_id = :sub, "
            "provider_price_id = :price, trial_ends_at = CASE WHEN :s = 'trialing' THEN trial_ends_at END, synced_at = now(), "
            "sync_error = :err WHERE tenant_id = :t"
        ),
        {
            "plan": plan,
            "s": status,
            "q": max(subscription.quantity, 1),
            "end": subscription.current_period_end,
            "cape": subscription.cancel_at_period_end,
            "ca": subscription.canceled_at,
            "pds": past_due_since,
            "sub": subscription.id,
            "price": subscription.price_id,
            "err": None if plan else "The subscription's price is not one of this application's plans.",
            "t": tenant_id,
        },
    )
    return f"{status} on {plan or 'an unknown price'}"


def sync(tenant_id: UUID) -> str:
    """Read the provider and apply it. The provider call happens outside any transaction."""
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        customer = customer_id(db, tenant_id)
    if customer is None:
        return "no billing customer"
    subscriptions = get_provider().subscriptions_for(customer)
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        return apply(db, tenant_id, _choose(subscriptions))


def reconcile_handler(db: Session, tenant_id: UUID, ref_id: UUID | None, payload: dict[str, Any]) -> datetime | None:
    db.commit()
    try:
        sync(tenant_id)
    except BillingError as exc:
        log.warning("billing_reconcile_failed", tenant_id=str(tenant_id), error=str(exc))
        return utcnow() + timedelta(hours=1)
    return utcnow() + timedelta(hours=24)


def handle_event(raw_body: bytes, signature: str) -> tuple[int, str]:
    """A callback from the provider. Returns the HTTP status to answer with and what happened."""
    settings = get_settings()
    if not verify_signature(settings.stripe_webhook_secret, signature, raw_body):
        return 400, "signature not valid"
    try:
        event = json.loads(raw_body)
        event_id, event_type = str(event["id"]), str(event["type"])
        obj = event["data"]["object"]
    except (ValueError, KeyError, TypeError):
        return 400, "not a billing event"
    customer = (
        obj.get("id") if event_type.startswith("customer.") and obj.get("object") == "customer" else obj.get("customer")
    )
    customer = customer if isinstance(customer, str) else None

    with session_scope() as db:
        seen = db.execute(
            text(
                "INSERT INTO billing_events (provider_event_id, event_type, provider_customer_id) VALUES (:id, :type, :c) "
                "ON CONFLICT (provider_event_id) DO NOTHING RETURNING provider_event_id"
            ),
            {"id": event_id, "type": event_type, "c": customer},
        ).scalar()
        if seen is None:
            done = db.execute(
                text("SELECT processed_at FROM billing_events WHERE provider_event_id = :id"), {"id": event_id}
            ).scalar()
            if done is not None:
                return 200, "duplicate"
        # The tenant comes from the mapping stored at checkout, never from the event.
        tenant_id = (
            db.execute(text("SELECT billing_customer_tenant(:c)"), {"c": customer}).scalar() if customer else None
        )

    outcome = "no known customer"
    if tenant_id is not None:
        if event_type == "checkout.session.completed" and obj.get("client_reference_id") not in (None, str(tenant_id)):
            outcome = "ignored: checkout was started for another workspace"
        else:
            try:
                outcome = sync(tenant_id)
            except BillingError:
                return 503, "provider unavailable; send again"  # not marked processed, so the retry does the work
    with session_scope() as db:
        db.execute(
            text("UPDATE billing_events SET processed_at = now(), outcome = :o WHERE provider_event_id = :id"),
            {"o": outcome, "id": event_id},
        )
    return 200, outcome
