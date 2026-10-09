"""Plans, entitlements and the billing provider. Stripe is a local stand-in; no account exists yet."""

import hashlib
import hmac
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.db import RlsContext, session_scope
from app.modules.billing import entitlements, service
from app.modules.billing.stripe import BillingError, FakeStripe, StripeProvider, verify_signature
from tests.helpers import API, create_workspace, invite, join, sign_in
from tests.test_import import ok

WEBHOOK_SECRET = "whsec_test_only_0000000000000000"
PRICES = {"test_starter": "price_test_starter", "test_team": "price_test_team"}


@pytest.fixture
def stripe(monkeypatch: pytest.MonkeyPatch, migrator_engine: Any) -> Any:
    """Billing switched on in test mode, with a stand-in provider and test prices attached to the test plans."""
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_mode", "test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    with migrator_engine.begin() as conn:
        for code, price in PRICES.items():
            conn.execute(text("UPDATE plans SET provider_price_id = :p WHERE code = :c"), {"p": price, "c": code})
    fake = FakeStripe()
    service.set_provider(fake)
    yield fake
    service.set_provider(None)
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE plans SET provider_price_id = NULL"))
        conn.execute(text("DELETE FROM billing_events"))


@pytest.fixture
def owner(make_client: Any, stripe: FakeStripe) -> TestClient:
    client = make_client()
    sign_in(client, "owner@example.test")
    create_workspace(client, "SEWEB")
    return client


@pytest.fixture
def tenant(owner: TestClient) -> UUID:
    return UUID(ok(owner.get(f"{API}/tenant"))["id"])


def sql(tenant: UUID, statement: str, **params: Any) -> Any:
    with session_scope(RlsContext(tenant_id=tenant)) as db:
        result = db.execute(text(statement), params)
        return [dict(r) for r in result.mappings()] if result.returns_rows else None


def event(client: TestClient, event_type: str, obj: dict[str, Any], *, event_id: str | None = None, secret: str = WEBHOOK_SECRET,
          at: int | None = None, tamper: bool = False) -> Any:  # fmt: skip
    body = json.dumps({"id": event_id or f"evt_{uuid4().hex}", "type": event_type, "data": {"object": obj}}).encode()
    stamp = at or int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    sent = body.replace(b'"data"', b'"data" ') if tamper else body
    return client.post(
        f"{API}/webhooks/stripe", content=sent, headers={"Stripe-Signature": f"t={stamp},v1={signature}"}
    )


def checkout(owner: TestClient, stripe: FakeStripe, plan: str = "test_starter", **body: Any) -> str:
    """Start checkout and return the customer the provider now knows. Nothing is granted by this."""
    url = ok(owner.post(f"{API}/billing/checkout", json={"plan_code": plan, **body}))["url"]
    assert url.startswith("https://checkout.stripe.test/")
    return str(stripe.checkouts[-1]["customer"])


def changed(client: TestClient, sub: Any, kind: str = "customer.subscription.updated") -> Any:
    return event(
        client, kind, {"id": sub.id, "object": "subscription", "customer": sub.customer_id, "status": sub.status}
    )


# --- standing --------------------------------------------------------------------------------


def test_billing_is_off_by_default_and_then_nothing_is_limited(make_client: Any) -> None:
    assert Settings.model_fields["billing_mode"].default == "off"
    client = make_client()
    sign_in(client, "owner@example.test")
    create_workspace(client, "SEWEB")
    standing = ok(client.get(f"{API}/entitlements"))
    assert (
        standing["billing_enforced"] is False
        and standing["standing"] == "good"
        and standing["plan_code"] == "internal_pilot"
    )
    assert all(limit is None for limit in standing["limits"].values())
    assert client.post(f"{API}/billing/checkout", json={"plan_code": "test_starter"}).status_code == 409
    with pytest.raises(ValueError, match="live Stripe key"):
        Settings(**{**get_settings().model_dump(), "stripe_secret_key": "sk_live_abc"})


def test_a_new_workspace_is_on_a_trial_and_plans_are_labelled_as_tests(owner: TestClient, tenant: UUID) -> None:
    billing = ok(owner.get(f"{API}/billing"))
    assert (billing["standing"], billing["status"], billing["plan_code"], billing["is_test_plan"]) == (
        "good",
        "trialing",
        "trial",
        True,
    )
    ends = datetime.fromisoformat(billing["trial_ends_at"])
    assert timedelta(days=13, hours=23) < ends - datetime.now(UTC) <= timedelta(days=14)
    assert billing["limits"]["seats"] == 3 and billing["usage"]["seats"] == 1 and billing["paid_overage"] == "never"
    assert [p["code"] for p in billing["plans"]] == ["test_starter", "test_team"]
    assert all(
        p["is_test"] and "not an approved price" in p["price_note"] and p["purchasable"] for p in billing["plans"]
    )


def test_an_expired_trial_restricts_writes_but_not_reading_export_or_billing(
    owner: TestClient, tenant: UUID, make_client: Any
) -> None:
    company = ok(owner.post(f"{API}/companies", json={"name": "Before the trial ended"}), 201)
    to_erase = ok(owner.post(f"{API}/companies", json={"name": "Asked to be forgotten"}), 201)
    created_key = ok(
        owner.post(f"{API}/api-keys", json={"name": "Agent", "scopes": ["crm.read", "crm.write", "outreach.draft"]}),
        201,
    )
    key = created_key["key"]
    hook = ok(owner.post(f"{API}/webhook-endpoints", json={"url": "https://hooks.customer.example/crm"}), 201)
    grant = ok(
        owner.post(
            f"{API}/support-grants", json={"grantee_email": "helper@example.test", "reason": "Looking into a problem"}
        ),
        201,
    )
    ok(owner.get(f"{API}/entitlements"))
    sql(tenant, "UPDATE tenant_billing SET trial_ends_at = now() - interval '1 minute'")

    standing = ok(owner.get(f"{API}/entitlements"))
    assert standing["standing"] == "restricted" and "trial has ended" in standing["reason"]
    for method, path, body in (
        ("POST", "/companies", {"name": "After"}),
        ("PATCH", f"/companies/{company['id']}", {"name": "Renamed"}),
        ("POST", "/tasks", {"title": "x"}),
        ("POST", "/invitations", {"email": "rep@example.test", "role": "representative"}),
        ("POST", "/api-keys", {"name": "Another", "scopes": ["crm.read"]}),
        ("POST", "/email-drafts", {"lead_id": company["id"]}),
        ("POST", "/webhook-endpoints", {"url": "https://hooks.customer.example/x"}),
        ("POST", "/support-grants", {"grantee_email": "x@example.test", "reason": "Looking into a problem"}),
    ):
        refused = owner.request(method, f"{API}{path}", json=body)
        assert refused.status_code == 402 and refused.json()["error"]["code"] == "subscription_required", path
    # The same rule for an API key: it is enforced on the server, not in the page.
    agent = make_client()
    agent.headers["Authorization"] = f"Bearer {key}"
    assert agent.post(f"{API}/companies", json={"name": "Through the API"}).status_code == 402
    assert agent.get(f"{API}/companies").status_code == 200

    # Nothing was deleted, and the owner can still read, export and reach billing.
    assert ok(owner.get(f"{API}/companies"))["total"] == 2
    assert owner.get(f"{API}/exports/prospects.xlsx").status_code == 200
    assert ok(owner.get(f"{API}/billing"))["standing"] == "restricted"
    assert ok(owner.post(f"{API}/billing/checkout", json={"plan_code": "test_starter"}))["url"]

    # Not having paid never stops a workspace from reducing access, stopping contact or removing data.
    assert (
        agent.post(f"{API}/email-suppressions", json={"value": "optout@example.bg", "reason": "opt_out"}).status_code
        == 201
    )  # by key too
    assert (
        agent.post(
            f"{API}/companies/{company['id']}/restrictions",
            json={"channel_kind": "phone", "reason": "Asked not to be called"},
        ).status_code
        == 201
    )
    for method, path, body, expected in (
        ("POST", "/email-suppressions", {"value": "another@example.bg"}, 201),
        ("PATCH", f"/webhook-endpoints/{hook['id']}", {"status": "paused"}, 200),
        ("DELETE", f"/webhook-endpoints/{hook['id']}", None, 204),
        ("DELETE", f"/support-grants/{grant['id']}", None, 200),
        ("PUT", "/retention", {"email_content_days": 30}, 200),
        (
            "POST",
            f"/companies/{to_erase['id']}/erase",
            {"reason": "Erasure requested by the business", "confirm_name": "Asked to be forgotten"},
            200,
        ),
        ("DELETE", f"/api-keys/{created_key['id']}", None, 200),
        ("POST", "/tenant/deletion", {"confirm_name": "SEWEB"}, 200),
        ("DELETE", "/tenant/deletion", None, 200),
    ):
        allowed = owner.request(method, f"{API}{path}", json=body)
        assert allowed.status_code == expected, (method, path, allowed.status_code, allowed.text[:200])
    assert agent.get(f"{API}/companies").status_code == 401  # the revoked key
    assert ok(owner.get(f"{API}/companies"))["total"] == 1
    # The allow-list is exact: a path that merely contains "billing" is not a way round it.
    assert owner.post(f"{API}/companies", json={"name": "billing"}).status_code == 402
    assert owner.post(f"{API}/tasks?note=/billing/", json={"title": "x"}).status_code == 402


def test_returning_from_checkout_grants_nothing_until_the_provider_confirms(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient
) -> None:
    sql(
        tenant,
        "INSERT INTO tenant_billing (tenant_id, plan_code, status, seats, trial_ends_at) VALUES (:t, 'trial', 'trialing', 3, now() - interval '1 day')",
        t=tenant,
    )
    customer = checkout(owner, stripe)
    assert stripe.checkouts[-1] == {"customer": customer, "price": "price_test_starter", "quantity": 3, "tenant_id": str(tenant),
                                    "success_url": stripe.checkouts[-1]["success_url"]}  # fmt: skip
    # The browser comes back to the success address. That alone changes nothing.
    assert owner.get("/billing?checkout=returned").status_code in (200, 404)
    assert ok(owner.get(f"{API}/billing"))["standing"] == "restricted"
    assert owner.post(f"{API}/companies", json={"name": "Not yet"}).status_code == 402
    # A "completed" event with no subscription behind it grants nothing either.
    assert (
        event(
            client,
            "checkout.session.completed",
            {"id": "cs_1", "customer": customer, "client_reference_id": str(tenant)},
        ).status_code
        == 200
    )
    assert ok(owner.get(f"{API}/billing"))["standing"] == "restricted"

    sub = stripe.subscribe(
        customer, "price_test_starter", quantity=3, current_period_end=datetime.now(UTC) + timedelta(days=30)
    )
    assert changed(client, sub, "customer.subscription.created").text == "active on test_starter"
    billing = ok(owner.get(f"{API}/billing"))
    assert (billing["standing"], billing["status"], billing["plan_code"], billing["limits"]["seats"]) == (
        "good",
        "active",
        "test_starter",
        3,
    )
    assert owner.post(f"{API}/companies", json={"name": "Now it works"}).status_code == 201


def test_the_client_cannot_choose_a_price_or_another_tenants_plan(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, migrator_engine: Any
) -> None:
    for body in ({"plan_code": "test_starter", "price_id": "price_one_cent"}, {"plan_code": "test_starter", "price": 1},
                 {"plan_code": "test_starter", "tenant_id": str(uuid4())}):  # fmt: skip
        assert owner.post(f"{API}/billing/checkout", json=body).status_code == 422
    assert (
        owner.post(f"{API}/billing/checkout", json={"plan_code": "trial"}).status_code == 404
    )  # not a purchasable plan
    assert owner.post(f"{API}/billing/checkout", json={"plan_code": "price_test_team"}).status_code == 404
    assert stripe.checkouts == []
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE plans SET provider_price_id = NULL WHERE code = 'test_team'"))
    refused = owner.post(f"{API}/billing/checkout", json={"plan_code": "test_team"})
    assert refused.status_code == 409 and "no price set up" in refused.json()["error"]["message"]
    # A subscription to a price that is not one of ours grants nothing.
    customer = checkout(owner, stripe)
    stripe.subscribe(customer, "price_someone_made_up")
    assert service.sync(tenant) == "unrecognized on an unknown price"
    billing = ok(owner.get(f"{API}/billing"))
    assert billing["standing"] == "restricted" and "does not recognise" in billing["reason"] and billing["sync_error"]


# --- events ----------------------------------------------------------------------------------


def test_callbacks_are_verified_on_the_raw_body(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient
) -> None:
    customer = checkout(owner, stripe)
    sub = stripe.subscribe(customer, "price_test_starter")
    obj = {"id": sub.id, "customer": customer}
    assert client.post(f"{API}/webhooks/stripe", content=b"{}").status_code == 400
    assert event(client, "customer.subscription.updated", obj, secret="whsec_wrong").status_code == 400
    assert event(client, "customer.subscription.updated", obj, tamper=True).status_code == 400  # one byte of whitespace
    assert event(client, "customer.subscription.updated", obj, at=int(time.time()) - 3600).status_code == 400
    assert ok(owner.get(f"{API}/billing"))["status"] == "trialing"  # none of those did anything
    assert event(client, "customer.subscription.updated", obj).status_code == 200
    assert ok(owner.get(f"{API}/billing"))["status"] == "active"

    body, stamp = b'{"a":1}', int(time.time())
    good = hmac.new(b"s", f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_signature("s", f"t={stamp},v1={good}", body) and verify_signature(
        "s", f"t={stamp},v1=bad,v1={good}", body
    )
    assert not verify_signature("s", f"v1={good}", body) and not verify_signature("", f"t={stamp},v1={good}", body)
    assert not verify_signature("s", f"t={stamp},t={stamp},v1={good}", body) and not verify_signature("s", "", body)


def test_duplicate_and_out_of_order_events_converge_on_the_providers_state(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient
) -> None:
    customer = checkout(owner, stripe)
    sub = stripe.subscribe(customer, "price_test_starter", quantity=3)
    created = {"id": sub.id, "customer": customer, "status": "active"}
    # The lifecycle at the provider: active -> past_due -> active -> cancelled.
    sub.status = "canceled"
    sub.canceled_at = datetime.now(UTC)
    # Events arrive late, repeated and backwards. Their payloads claim older states.
    for event_type, claimed, event_id in (
        ("customer.subscription.deleted", "canceled", "evt_4"),
        ("invoice.paid", "active", "evt_3"),
        ("customer.subscription.updated", "past_due", "evt_2"),
        ("customer.subscription.created", "active", "evt_1"),
        ("customer.subscription.created", "active", "evt_1"),
        ("invoice.paid", "active", "evt_3"),
    ):
        assert event(client, event_type, {**created, "status": claimed}, event_id=event_id).status_code == 200
        assert ok(owner.get(f"{API}/billing"))["status"] == "canceled", event_type  # what the provider says now
    assert event(client, "invoice.paid", created, event_id="evt_3").text == "duplicate"
    assert ok(owner.get(f"{API}/billing"))["standing"] == "restricted"
    assert sql(tenant, "SELECT count(*) AS n FROM tenant_billing")[0]["n"] == 1

    # The provider is unreachable when an event arrives: nothing is marked done, so the retry applies it.
    again = stripe.subscribe(customer, "price_test_team", quantity=5)
    stripe.down = True
    assert changed(client, again).status_code == 503
    assert ok(owner.get(f"{API}/billing"))["status"] == "canceled"
    stripe.down = False
    assert event(client, "customer.subscription.updated", {"id": again.id, "customer": customer}).status_code == 200
    billing = ok(owner.get(f"{API}/billing"))
    assert (billing["status"], billing["plan_code"], billing["limits"]["seats"]) == ("active", "test_team", 5)


def test_an_event_cannot_bind_a_subscription_to_the_wrong_tenant(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient, make_client: Any
) -> None:
    other = make_client()
    sign_in(other, "other@example.test")
    other_tenant = UUID(create_workspace(other, "Another customer"))
    paying = checkout(other, stripe, "test_team")
    sub = stripe.subscribe(paying, "price_test_team", quantity=10)
    mine = checkout(owner, stripe)

    # 1. An event naming the paying customer but claiming this tenant: the tenant comes from the stored mapping.
    forged = {
        "id": sub.id,
        "customer": paying,
        "client_reference_id": str(tenant),
        "metadata": {"tenant_id": str(tenant)},
    }
    assert event(client, "customer.subscription.updated", forged).status_code == 200
    assert (
        ok(owner.get(f"{API}/billing"))["status"] == "trialing"
        and ok(other.get(f"{API}/billing"))["status"] == "active"
    )
    # 2. A checkout completed for the other workspace, replayed against this customer.
    crossed = event(
        client, "checkout.session.completed", {"id": "cs_x", "customer": mine, "client_reference_id": str(other_tenant)}
    )
    assert crossed.text == "ignored: checkout was started for another workspace"
    # 3. The other workspace's subscription attached to this customer at the provider (by mistake or malice).
    stolen = stripe.subscribe(mine, "price_test_team", quantity=10)
    stolen.metadata = {"tenant_id": str(other_tenant)}
    assert changed(client, stolen).text == "ignored: belongs to another workspace"
    mine_now = ok(owner.get(f"{API}/billing"))
    assert (
        mine_now["status"] == "trialing"
        and mine_now["plan_code"] == "trial"
        and "another workspace" in mine_now["sync_error"]
    )
    # 4. A customer nobody knows.
    assert (
        event(client, "customer.subscription.updated", {"id": "sub_x", "customer": "cus_unknown"}).text
        == "no known customer"
    )
    # 5. A workspace cannot see or act on another's billing.
    assert sql(other_tenant, "SELECT count(*) AS n FROM billing_customers")[0]["n"] == 1
    assert sql(tenant, "SELECT provider_customer_id FROM billing_customers")[0]["provider_customer_id"] == mine


def test_past_due_has_a_grace_period_then_restricts_and_paying_restores(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient
) -> None:
    customer = checkout(owner, stripe)
    sub = stripe.subscribe(customer, "price_test_starter", quantity=3)
    changed(client, sub)
    sub.status = "past_due"
    assert event(client, "invoice.payment_failed", {"id": "in_1", "customer": customer}).status_code == 200
    billing = ok(owner.get(f"{API}/billing"))
    assert billing["standing"] == "grace" and "overdue" in billing["reason"]
    grace = datetime.fromisoformat(billing["grace_until"]) - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < grace <= timedelta(days=7)
    assert owner.post(f"{API}/companies", json={"name": "Still working in grace"}).status_code == 201
    changed(client, sub)  # a second failure notice does not restart the clock
    assert datetime.fromisoformat(ok(owner.get(f"{API}/billing"))["grace_until"]) == datetime.fromisoformat(
        billing["grace_until"]
    )

    sql(tenant, "UPDATE tenant_billing SET past_due_since = now() - interval '8 days'")
    assert ok(owner.get(f"{API}/billing"))["standing"] == "restricted"
    assert owner.post(f"{API}/companies", json={"name": "Blocked"}).status_code == 402
    assert ok(owner.get(f"{API}/companies"))["total"] == 1  # nothing deleted
    assert ok(owner.post(f"{API}/billing/portal"))["url"].endswith(customer)

    sub.status = "active"
    changed(client, sub)
    restored = ok(owner.get(f"{API}/billing"))
    assert restored["standing"] == "good" and restored["grace_until"] is None
    assert owner.post(f"{API}/companies", json={"name": "Back"}).status_code == 201


def test_a_refusal_for_payment_is_not_remembered_against_an_idempotency_key(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient, make_client: Any
) -> None:
    key = ok(owner.post(f"{API}/api-keys", json={"name": "Agent", "scopes": ["crm.read", "crm.write"]}), 201)["key"]
    agent = make_client()
    agent.headers["Authorization"] = f"Bearer {key}"
    ok(owner.get(f"{API}/entitlements"))
    sql(tenant, "UPDATE tenant_billing SET trial_ends_at = now() - interval '1 minute'")
    headers = {"Idempotency-Key": "create-acme"}
    assert agent.post(f"{API}/companies", json={"name": "Acme"}, headers=headers).status_code == 402
    customer = checkout(owner, stripe)
    changed(client, stripe.subscribe(customer, "price_test_starter", quantity=3))
    paid = agent.post(f"{API}/companies", json={"name": "Acme"}, headers=headers)
    assert (
        paid.status_code == 201 and "idempotency-replayed" not in paid.headers
    )  # the same key works once the cause has passed
    assert (
        agent.post(f"{API}/companies", json={"name": "Acme"}, headers=headers).headers["idempotency-replayed"] == "true"
    )
    # A workspace with a running subscription cannot be scheduled for deletion: it would keep being charged.
    refused = owner.post(f"{API}/tenant/deletion", json={"confirm_name": "SEWEB"})
    assert refused.status_code == 409 and "Cancel it in billing first" in refused.json()["error"]["message"]


def test_cancellation_takes_effect_at_the_end_of_the_paid_period(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient
) -> None:
    customer = checkout(owner, stripe)
    period_end = datetime.now(UTC).replace(microsecond=0) + timedelta(days=12)
    sub = stripe.subscribe(customer, "price_test_starter", quantity=3, current_period_end=period_end)
    sub.cancel_at_period_end = True
    changed(client, sub)
    billing = ok(owner.get(f"{API}/billing"))
    assert billing["standing"] == "good" and billing["cancel_at_period_end"] is True
    assert (
        datetime.fromisoformat(billing["current_period_end"]) == period_end and "will not renew" in billing["notes"][0]
    )
    assert owner.post(f"{API}/companies", json={"name": "Paid until the end"}).status_code == 201
    sub.status, sub.canceled_at = "canceled", period_end
    changed(client, sub, "customer.subscription.deleted")
    assert ok(owner.get(f"{API}/billing"))["standing"] == "restricted"
    assert ok(owner.get(f"{API}/companies"))["total"] == 1


# --- limits ----------------------------------------------------------------------------------


def test_concurrent_invitations_cannot_exceed_the_seats(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    # Three seats on the trial; the owner uses one. Eight invitations at once: exactly two succeed.
    cookies, csrf = dict(owner.cookies), owner.headers["X-CSRF-Token"]
    statuses: list[int] = []
    barrier = threading.Barrier(8)

    def send(n: int) -> None:
        client = make_client()
        client.cookies.update(cookies)
        client.headers["X-CSRF-Token"] = csrf
        barrier.wait()
        statuses.append(
            client.post(
                f"{API}/invitations", json={"email": f"person{n}@example.test", "role": "representative"}
            ).status_code
        )

    threads = [threading.Thread(target=send, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [201, 201, 409, 409, 409, 409, 409, 409]
    standing = ok(owner.get(f"{API}/entitlements"))
    assert standing["usage"]["seats"] == 3 and standing["limits"]["seats"] == 3 and standing["over_limit"] == []
    refused = owner.post(f"{API}/invitations", json={"email": "late@example.test", "role": "representative"})
    assert (
        refused.json()["error"]["code"] == "plan_limit_reached"
        and "Nothing extra is charged" in refused.json()["error"]["message"]
    )


def test_a_downgrade_below_current_use_keeps_everything_and_blocks_only_growth(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, client: TestClient, make_client: Any
) -> None:
    customer = checkout(owner, stripe, "test_team", seats=10)
    sub = stripe.subscribe(customer, "price_test_team", quantity=10)
    changed(client, sub)
    members = []
    for email, role in (
        ("admin@example.test", "administrator"),
        ("manager@example.test", "sales_manager"),
        ("rep@example.test", "representative"),
    ):
        member = make_client()
        sign_in(member, email)
        join(owner, member, email, role)
        members.append(member)
    invite(owner, "viewer@example.test", "read_only")
    keys = [ok(owner.post(f"{API}/api-keys", json={"name": f"k{n}", "scopes": ["crm.read"]}), 201) for n in range(3)]
    assert (
        owner.post(f"{API}/billing/checkout", json={"plan_code": "test_starter", "seats": 2}).status_code == 422
    )  # five in use

    sub.price_id, sub.quantity = "price_test_starter", 3  # downgraded in the portal
    changed(client, sub)
    billing = ok(owner.get(f"{API}/billing"))
    assert billing["standing"] == "good" and billing["plan_code"] == "test_starter"
    assert billing["usage"]["seats"] == 5 and billing["limits"]["seats"] == 3 and billing["usage"]["api_keys"] == 3
    assert (
        set(billing["over_limit"]) == {"members and open invitations", "API keys"}
        and "Nothing is removed" in billing["notes"][-1]
    )
    # Everyone still has access and existing keys still work; only adding more is refused.
    assert all(m.get(f"{API}/companies").status_code == 200 for m in members)
    assert len(ok(owner.get(f"{API}/members"))["items"]) == 4 and len(ok(owner.get(f"{API}/api-keys"))) == 3
    agent = make_client()
    agent.headers["Authorization"] = f"Bearer {keys[0]['key']}"
    assert agent.get(f"{API}/companies").status_code == 200
    assert (
        owner.post(f"{API}/invitations", json={"email": "extra@example.test", "role": "read_only"}).status_code == 409
    )
    assert owner.post(f"{API}/api-keys", json={"name": "one more", "scopes": ["crm.read"]}).status_code == 409
    assert members[2].post(f"{API}/companies", json={"name": "Work goes on"}).status_code == 201


def test_workers_recheck_the_standing_before_spending_or_sending(
    owner: TestClient, tenant: UUID, stripe: FakeStripe, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.discovery import orchestrator
    from app.modules.email import dispatch

    # A research run queued while in good standing does no paid work after the workspace is restricted.
    discovery = ok(
        owner.post(
            f"{API}/provider-connections",
            json={"provider": "fake_discovery", "label": "d", "credential": "local-test-key-1"},
        ),
        201,
    )
    model = ok(
        owner.post(
            f"{API}/provider-connections",
            json={"provider": "fake_model", "label": "m", "credential": "local-test-key-2"},
        ),
        201,
    )
    ok(owner.put(f"{API}/budgets", json={"scope": "research", "limit_amount": "5"}))
    config = ok(owner.post(f"{API}/research-configs", json={
        "name": "c", "country": "Bulgaria", "cities": ["Sofia"], "categories": ["salon"], "cost_cap": "1", "candidate_cap": 5,
        "qualified_target": 2, "discovery_connection_id": discovery["id"], "model_connection_id": model["id"]}), 201)  # fmt: skip
    run = ok(owner.post(f"{API}/research-configs/{config['id']}/runs", json={}), 201)
    sql(tenant, "UPDATE tenant_billing SET trial_ends_at = now() - interval '1 minute'")
    assert orchestrator.run_step(tenant, UUID(run["id"])) == "paused"
    paused = ok(owner.get(f"{API}/research-runs/{run['id']}"))
    assert paused["status"] == "paused" and "trial has ended" in paused["error"]
    assert ok(owner.get(f"{API}/usage/summary"))["entries"] == 0  # nothing was reserved or spent
    assert owner.post(f"{API}/research-configs/{config['id']}/runs", json={}).status_code in (402, 409)

    # A send that has not started is stopped by the same check.
    sql(tenant, "UPDATE tenant_billing SET trial_ends_at = now() + interval '5 days'")
    monkeypatch.setattr(get_settings(), "email_dispatch", "live")
    mailbox, draft, intent = uuid4(), uuid4(), uuid4()
    sql(
        tenant,
        "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status) VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active')",
        m=mailbox,
        t=tenant,
    )
    sql(
        tenant,
        "INSERT INTO email_drafts (id, tenant_id, mailbox_id, kind, to_address, subject, body_text, status) VALUES (:d, :t, :m, 'reply', 'a@example.bg', 's', 'b', 'queued')",
        d=draft,
        t=tenant,
        m=mailbox,
    )
    sql(tenant, "INSERT INTO send_intents (id, tenant_id, draft_id, mailbox_id, draft_version, content_hash, to_address, kind, scheduled_for, rfc_message_id) "
                "VALUES (:i, :t, :d, :m, 1, 'h', 'a@example.bg', 'reply', now(), '<x@seweb.example>')", i=intent, t=tenant, d=draft, m=mailbox)  # fmt: skip
    sql(tenant, "UPDATE tenant_billing SET trial_ends_at = now() - interval '1 minute'")
    assert dispatch.process(tenant, intent) is None
    stopped = sql(tenant, "SELECT state, state_reason FROM send_intents")[0]
    assert stopped["state"] == "blocked" and "trial has ended" in stopped["state_reason"]


def test_monthly_limits_refuse_without_charging(owner: TestClient, tenant: UUID, migrator_engine: Any) -> None:
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE plans SET research_runs_per_month = 1 WHERE code = 'trial'"))
    try:
        discovery = ok(
            owner.post(
                f"{API}/provider-connections",
                json={"provider": "fake_discovery", "label": "d", "credential": "local-test-key-1"},
            ),
            201,
        )
        model = ok(
            owner.post(
                f"{API}/provider-connections",
                json={"provider": "fake_model", "label": "m", "credential": "local-test-key-2"},
            ),
            201,
        )
        configs = [ok(owner.post(f"{API}/research-configs", json={
            "name": f"c{n}", "country": "Bulgaria", "cities": ["Sofia"], "categories": ["salon"], "cost_cap": "1", "candidate_cap": 5,
            "qualified_target": 2, "discovery_connection_id": discovery["id"], "model_connection_id": model["id"]}), 201) for n in range(2)]  # fmt: skip
        assert owner.post(f"{API}/research-configs/{configs[0]['id']}/runs", json={}).status_code == 201
        refused = owner.post(f"{API}/research-configs/{configs[1]['id']}/runs", json={})
        assert (
            refused.status_code == 409
            and "the plan allows 1 research runs this month" in refused.json()["error"]["message"]
        )
        standing = ok(owner.get(f"{API}/entitlements"))
        assert (
            standing["usage"]["research_runs_this_month"] == 1 and standing["limits"]["research_runs_this_month"] == 1
        )
    finally:
        with migrator_engine.begin() as conn:
            conn.execute(text("UPDATE plans SET research_runs_per_month = 5 WHERE code = 'trial'"))


def test_only_the_owner_reaches_billing(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    for method, path in (
        ("GET", "/billing"),
        ("POST", "/billing/checkout"),
        ("POST", "/billing/portal"),
        ("POST", "/billing/sync"),
    ):
        assert admin.request(method, f"{API}{path}", json={"plan_code": "test_starter"}).status_code == 403, path
    assert ok(admin.get(f"{API}/entitlements"))["standing"] == "good"  # every member can see where the workspace stands
    key = ok(owner.post(f"{API}/api-keys", json={"name": "Agent", "scopes": ["crm.read", "crm.write"]}), 201)["key"]
    agent = make_client()
    agent.headers["Authorization"] = f"Bearer {key}"
    assert (
        agent.get(f"{API}/billing").status_code == 403
        and agent.post(f"{API}/billing/checkout", json={"plan_code": "test_team"}).status_code == 403
    )
    assert owner.post(f"{API}/billing/portal").status_code == 409  # no billing account yet


def test_stripe_adapter_contract() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.headers["Authorization"] != "Bearer sk_test_abc":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        if path == "/v1/customers":
            return httpx.Response(200, json={"id": "cus_1"})
        if path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})
        if path == "/v1/billing_portal/sessions":
            return httpx.Response(200, json={"url": "https://billing.stripe.com/p/session/1"})
        if path == "/v1/subscriptions/sub_gone":
            return httpx.Response(404, json={"error": {"message": "No such subscription"}})
        subscription = {"id": "sub_1", "customer": "cus_1", "status": "past_due", "cancel_at_period_end": True, "canceled_at": None,
                        "metadata": {"tenant_id": "t-1"}, "items": {"data": [{"price": {"id": "price_1"}, "quantity": 4,
                                                                               "current_period_end": 1793000000}]}}  # fmt: skip
        if path == "/v1/subscriptions/sub_1":
            return httpx.Response(200, json=subscription)
        if path == "/v1/subscriptions":
            return httpx.Response(200, json={"data": [subscription]})
        return httpx.Response(500)

    provider = StripeProvider("sk_test_abc", httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.create_customer(name="SEWEB", tenant_id="t-1") == "cus_1"
    assert (
        calls[-1].headers["Idempotency-Key"] == "customer-t-1" and b"metadata%5Btenant_id%5D=t-1" in calls[-1].content
    )
    url = provider.create_checkout(customer_id="cus_1", price_id="price_1", quantity=4, tenant_id="t-1",
                                   success_url="https://crm.example/billing", cancel_url="https://crm.example/billing")  # fmt: skip
    form = dict(pair.split("=", 1) for pair in calls[-1].content.decode().split("&"))
    assert (
        url.startswith("https://checkout.stripe.com/")
        and form["mode"] == "subscription"
        and form["client_reference_id"] == "t-1"
    )
    assert form["line_items%5B0%5D%5Bprice%5D"] == "price_1" and form["line_items%5B0%5D%5Bquantity%5D"] == "4"
    assert form["subscription_data%5Bmetadata%5D%5Btenant_id%5D"] == "t-1"
    assert provider.create_portal(customer_id="cus_1", return_url="https://crm.example/billing").startswith(
        "https://billing.stripe.com/"
    )
    sub = provider.subscription("sub_1")
    assert sub is not None and (sub.status, sub.price_id, sub.quantity, sub.cancel_at_period_end) == (
        "past_due",
        "price_1",
        4,
        True,
    )
    assert sub.current_period_end == datetime.fromtimestamp(1793000000, tz=UTC) and sub.metadata == {"tenant_id": "t-1"}
    assert provider.subscription("sub_gone") is None and [s.id for s in provider.subscriptions_for("cus_1")] == [
        "sub_1"
    ]
    with pytest.raises(BillingError, match="refused"):
        StripeProvider("sk_test_wrong", httpx.Client(transport=httpx.MockTransport(handler))).create_customer(
            name="x", tenant_id="t"
        )
    assert all(str(c.url).startswith("https://api.stripe.com/v1/") for c in calls)
    assert entitlements.PILOT["allow_paid_overage"] is False
