"""Plans, the workspace's standing, checkout and the billing portal."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import get_settings
from app.core.deps import Tenant, TenantContext, tenant_with
from app.core.errors import ConflictError, ServiceUnavailableError
from app.modules.billing import entitlements, service
from app.modules.billing.stripe import BillingError
from app.modules.crm.common import ValidationFailed, many, one
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission

router = APIRouter()
public_router = APIRouter()
BILLING = tenant_with(Permission.TENANT_BILLING)


class PlanOut(BaseModel):
    code: str
    name: str
    is_test: bool
    price_note: str = Field(description="Shown as written. A test plan says so here.")
    seats_included: int
    research_runs_per_month: int
    emails_per_month: int
    api_keys: int
    webhook_endpoints: int
    purchasable: bool


class EntitlementOut(BaseModel):
    billing_enforced: bool
    standing: Literal["good", "grace", "restricted"]
    reason: str | None
    plan_code: str
    plan_name: str
    is_test_plan: bool
    status: str
    trial_ends_at: datetime | None
    grace_until: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    limits: dict[str, int | None] = Field(description="None means no limit applies")
    usage: dict[str, int]
    over_limit: list[str] = Field(
        description="Limits currently exceeded, for example after a downgrade. Nothing is deleted."
    )
    notes: list[str]


class BillingOut(EntitlementOut):
    plans: list[PlanOut]
    has_billing_customer: bool
    sync_error: str | None
    paid_overage: Literal["never"] = "never"


class CheckoutIn(BaseModel):
    model_config = {"extra": "forbid"}
    plan_code: str = Field(max_length=40)
    seats: int | None = Field(default=None, ge=1, le=500)


class RedirectOut(BaseModel):
    url: str


def _summary(ctx: TenantContext) -> dict[str, Any]:
    entitlement = entitlements.evaluate(ctx.db, ctx.tenant_id)
    used = entitlements.usage(ctx.db, ctx.tenant_id)
    limits: dict[str, int | None] = {}
    over: list[str] = []
    for counter, (_, label) in entitlements.LIMITS.items():
        allowed = entitlements.allowance(entitlement, counter)
        limits[counter] = None if allowed >= entitlements.UNLIMITED else allowed
        if allowed < entitlements.UNLIMITED and used[counter] > allowed:
            over.append(label)
    notes = list(entitlement.notes)
    if over:
        notes.append(
            "Over the plan's limits. Nothing is removed; new ones cannot be added until usage is back within the plan."
        )
    return {
        "billing_enforced": entitlement.billing_enforced,
        "standing": entitlement.standing,
        "reason": entitlement.reason,
        "plan_code": entitlement.plan["code"],
        "plan_name": entitlement.plan["name"],
        "is_test_plan": bool(entitlement.plan["is_test"]),
        "status": entitlement.status,
        "trial_ends_at": entitlement.trial_ends_at,
        "grace_until": entitlement.grace_until,
        "current_period_end": entitlement.current_period_end,
        "cancel_at_period_end": entitlement.cancel_at_period_end,
        "limits": limits,
        "usage": used,
        "over_limit": over,
        "notes": notes,
    }


@router.get("/entitlements", response_model=EntitlementOut, operation_id="getEntitlements", tags=["billing"])
def get_entitlements(ctx: Tenant) -> EntitlementOut:
    """What this workspace may do now, for every member."""
    return EntitlementOut(**_summary(ctx))


@router.get("/billing", response_model=BillingOut, operation_id="getBilling", tags=["billing"])
def get_billing(ctx: TenantContext = BILLING) -> BillingOut:
    plans = many(ctx, "SELECT * FROM plans WHERE is_public ORDER BY position")
    row = ctx.db.execute(
        text("SELECT sync_error FROM tenant_billing WHERE tenant_id = :t"), {"t": ctx.tenant_id}
    ).first()
    return BillingOut(
        **_summary(ctx),
        plans=[PlanOut(**p, purchasable=p["provider_price_id"] is not None) for p in plans],
        has_billing_customer=service.customer_id(ctx.db, ctx.tenant_id) is not None,
        sync_error=row.sync_error if row else None,
    )


def _enabled() -> None:
    if get_settings().billing_mode == "off":
        raise ConflictError("Billing is switched off on this installation.")


@router.post("/billing/checkout", response_model=RedirectOut, operation_id="startCheckout", tags=["billing"])
def start_checkout(body: CheckoutIn, ctx: TenantContext = BILLING) -> RedirectOut:
    """Open the provider's checkout for one of this application's plans. Returning from it grants nothing by itself."""
    _enabled()
    # The client names a plan; the price is this application's, never the client's.
    plan = one(ctx, "SELECT * FROM plans WHERE code = :c AND is_public", {"c": body.plan_code}, "Plan not found.")
    if plan["provider_price_id"] is None:
        raise ConflictError("This plan has no price set up with the billing provider yet.")
    in_use = entitlements.usage(ctx.db, ctx.tenant_id)["seats"]
    seats = body.seats or max(in_use, plan["seats_included"])
    if seats < in_use:
        raise ValidationFailed(f"{in_use} seats are in use. Choose at least that many, or remove members first.")
    base = get_settings().public_base_url.rstrip("/")
    try:
        customer = service.ensure_customer(ctx.db, ctx.tenant_id)
        url = service.get_provider().create_checkout(
            customer_id=customer,
            price_id=plan["provider_price_id"],
            quantity=seats,
            tenant_id=str(ctx.tenant_id),
            success_url=f"{base}/billing?checkout=returned",
            cancel_url=f"{base}/billing?checkout=cancelled",
        )
    except BillingError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="billing.checkout_started",
        target_type="tenant",
        target_id=str(ctx.tenant_id),
        data={"plan": plan["code"], "seats": seats},
    )
    return RedirectOut(url=url)


@router.post("/billing/portal", response_model=RedirectOut, operation_id="openBillingPortal", tags=["billing"])
def open_portal(ctx: TenantContext = BILLING) -> RedirectOut:
    """Open the provider's portal: payment method, invoices, cancellation."""
    _enabled()
    customer = service.customer_id(ctx.db, ctx.tenant_id)
    if customer is None:
        raise ConflictError("There is no billing account for this workspace yet. Choose a plan first.")
    try:
        url = service.get_provider().create_portal(
            customer_id=customer, return_url=f"{get_settings().public_base_url.rstrip('/')}/billing"
        )
    except BillingError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    return RedirectOut(url=url)


@router.post("/billing/sync", response_model=BillingOut, operation_id="syncBilling", tags=["billing"])
def sync_now(ctx: TenantContext = BILLING) -> BillingOut:
    """Read the subscription from the billing provider now."""
    _enabled()
    ctx.db.commit()
    try:
        service.sync(ctx.tenant_id)
    except BillingError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    return get_billing(ctx)


@public_router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> Response:
    """Verified on the raw body. The tenant comes from the stored customer mapping, never from the payload."""
    from starlette.concurrency import run_in_threadpool

    raw = await request.body()
    status, outcome = await run_in_threadpool(service.handle_event, raw, request.headers.get("Stripe-Signature", ""))
    return Response(content=outcome, status_code=status, media_type="text/plain")
