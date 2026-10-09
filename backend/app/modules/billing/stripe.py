"""The billing provider: Stripe's REST API, and a stand-in for tests.

Only what the application needs is modelled: a customer, a Checkout session, a billing
portal session, reading a subscription, and verifying a webhook signature on the raw body.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx

API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_SECONDS = 300


class BillingError(Exception):
    """The provider could not be reached or refused the request."""


@dataclass
class Subscription:
    id: str
    customer_id: str
    status: str
    price_id: str | None
    quantity: int
    current_period_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    metadata: dict[str, str] = field(default_factory=dict)


class BillingProvider(Protocol):
    def create_customer(self, *, name: str, tenant_id: str) -> str: ...
    def create_checkout(
        self, *, customer_id: str, price_id: str, quantity: int, tenant_id: str, success_url: str, cancel_url: str
    ) -> str: ...
    def create_portal(self, *, customer_id: str, return_url: str) -> str: ...
    def subscription(self, subscription_id: str) -> Subscription | None: ...
    def subscriptions_for(self, customer_id: str) -> list[Subscription]: ...


def verify_signature(secret: str, header: str, raw_body: bytes, *, now: int | None = None) -> bool:
    """Stripe's scheme: ``t=<unix>,v1=<hmac-sha256 of "<t>.<raw body>">``. The body must be the bytes received."""
    if not secret or not header:
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    stamps = [v for k, v in parts if k == "t"]
    if len(stamps) != 1 or not stamps[0].isdigit():
        return False
    if abs((now or int(time.time())) - int(stamps[0])) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), stamps[0].encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in parts if k == "v1")


def _at(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if value else None


def parse_subscription(data: dict[str, Any]) -> Subscription:
    items = (data.get("items") or {}).get("data") or []
    first = items[0] if items else {}
    return Subscription(
        id=data["id"],
        customer_id=data["customer"] if isinstance(data["customer"], str) else data["customer"]["id"],
        status=data["status"],
        price_id=(first.get("price") or {}).get("id"),
        quantity=int(first.get("quantity") or 1),
        current_period_end=_at(first.get("current_period_end") or data.get("current_period_end")),
        cancel_at_period_end=bool(data.get("cancel_at_period_end")),
        canceled_at=_at(data.get("canceled_at")),
        metadata=dict(data.get("metadata") or {}),
    )


class StripeProvider:
    """Contract-tested against a mocked transport only. It has never been run against Stripe."""

    def __init__(self, secret_key: str, http: httpx.Client | None = None) -> None:
        self._key = secret_key
        self._http = http or httpx.Client(timeout=20)

    def _call(
        self, method: str, path: str, data: dict[str, Any] | None = None, idempotency_key: str | None = None
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http.request(method, f"{API}{path}", data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise BillingError("The billing provider could not be reached.") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise BillingError(f"The billing provider refused the request ({response.status_code}).")
        return response.json()

    def create_customer(self, *, name: str, tenant_id: str) -> str:
        created = self._call(
            "POST",
            "/customers",
            {"name": name, "metadata[tenant_id]": tenant_id},
            idempotency_key=f"customer-{tenant_id}",
        )
        return str(created["id"])

    def create_checkout(
        self, *, customer_id: str, price_id: str, quantity: int, tenant_id: str, success_url: str, cancel_url: str
    ) -> str:
        session = self._call(
            "POST",
            "/checkout/sessions",
            {
                "mode": "subscription",
                "customer": customer_id,
                "client_reference_id": tenant_id,
                "line_items[0][price]": price_id,
                "line_items[0][quantity]": str(quantity),
                "subscription_data[metadata][tenant_id]": tenant_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
            },
        )
        return str(session["url"])

    def create_portal(self, *, customer_id: str, return_url: str) -> str:
        session = self._call("POST", "/billing_portal/sessions", {"customer": customer_id, "return_url": return_url})
        return str(session["url"])

    def subscription(self, subscription_id: str) -> Subscription | None:
        data = self._call("GET", f"/subscriptions/{subscription_id}")
        return parse_subscription(data) if data else None

    def subscriptions_for(self, customer_id: str) -> list[Subscription]:
        data = self._call("GET", f"/subscriptions?customer={customer_id}&status=all&limit=10")
        return [parse_subscription(item) for item in (data or {}).get("data", [])]


class FakeStripe:
    """An in-memory account. Tests change subscriptions here, then deliver events in any order."""

    def __init__(self) -> None:
        self.customers: dict[str, dict[str, str]] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.checkouts: list[dict[str, Any]] = []
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise BillingError("The billing provider could not be reached.")

    def create_customer(self, *, name: str, tenant_id: str) -> str:
        self._check()
        customer_id = f"cus_test_{uuid4().hex[:12]}"
        self.customers[customer_id] = {"name": name, "tenant_id": tenant_id}
        return customer_id

    def create_checkout(
        self, *, customer_id: str, price_id: str, quantity: int, tenant_id: str, success_url: str, cancel_url: str
    ) -> str:
        self._check()
        self.checkouts.append(
            {
                "customer": customer_id,
                "price": price_id,
                "quantity": quantity,
                "tenant_id": tenant_id,
                "success_url": success_url,
            }
        )
        return f"https://checkout.stripe.test/session/{len(self.checkouts)}"

    def create_portal(self, *, customer_id: str, return_url: str) -> str:
        self._check()
        return f"https://billing.stripe.test/portal/{customer_id}"

    def subscription(self, subscription_id: str) -> Subscription | None:
        self._check()
        return self.subscriptions.get(subscription_id)

    def subscriptions_for(self, customer_id: str) -> list[Subscription]:
        self._check()
        return [s for s in self.subscriptions.values() if s.customer_id == customer_id]

    def subscribe(
        self, customer_id: str, price_id: str, *, quantity: int = 1, status: str = "active", **extra: Any
    ) -> Subscription:
        tenant_id = self.customers.get(customer_id, {}).get("tenant_id", "")
        sub = Subscription(
            id=f"sub_test_{uuid4().hex[:12]}",
            customer_id=customer_id,
            status=status,
            price_id=price_id,
            quantity=quantity,
            current_period_end=extra.pop("current_period_end", None),
            cancel_at_period_end=False,
            canceled_at=None,
            metadata={"tenant_id": tenant_id},
        )
        for name, value in extra.items():
            setattr(sub, name, value)
        self.subscriptions[sub.id] = sub
        return sub
