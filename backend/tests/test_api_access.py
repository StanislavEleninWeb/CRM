"""API keys, idempotency and outbound webhooks. No real receiver is contacted."""

import json
import threading
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core import outbox
from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.modules.discovery.fetcher import guarded_client
from app.modules.integrations import webhooks
from app.worker import due
from tests.helpers import API, create_workspace, join, sign_in
from tests.test_import import import_and_commit, lead_row, ok, workbook_bytes

READ = ["crm.read", "reports.read"]
WRITE = ["crm.read", "crm.write"]
EVERYTHING = ["crm.read", "crm.write", "research.run", "outreach.draft", "outreach.send", "reports.read"]


@pytest.fixture
def owner(make_client: Any) -> TestClient:
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


def new_key(client: TestClient, scopes: list[str], **extra: Any) -> dict[str, Any]:
    return ok(client.post(f"{API}/api-keys", json={"name": "Hermes", "scopes": scopes, **extra}), 201)  # type: ignore[no-any-return]


def with_key(make_client: Any, key: str) -> TestClient:
    """A client with no cookies at all: only the key."""
    client: TestClient = make_client()
    client.headers["Authorization"] = f"Bearer {key}"
    return client


# --- keys ------------------------------------------------------------------------------------


def test_a_key_is_shown_once_stored_as_a_hash_and_limited_to_its_scopes(
    owner: TestClient, tenant: UUID, make_client: Any
) -> None:
    created = new_key(owner, READ)
    key = created["key"]
    assert key.startswith("crm_") and created["prefix"] == key[:10] and created["usable"] is True
    assert created["expires_at"] is not None  # ninety days unless asked otherwise
    listed = ok(owner.get(f"{API}/api-keys"))
    assert "key" not in listed[0] and key not in json.dumps(listed)
    stored = sql(tenant, "SELECT * FROM api_keys")[0]
    assert key.encode() not in bytes(stored["key_hash"]) and key not in str(stored)
    assert key not in json.dumps(ok(owner.get(f"{API}/audit-events")))

    reader = with_key(make_client, key)
    assert ok(reader.get(f"{API}/companies"))["total"] == 0
    assert ok(reader.get(f"{API}/tenant"))["id"] == str(tenant)
    for method, path, body in (
        ("POST", "/companies", {"name": "Acme"}),
        ("POST", "/tasks", {"title": "Call back"}),
        ("POST", "/email-drafts", {"lead_id": str(tenant)}),
        ("POST", f"/email-drafts/{tenant}/send", {}),
        ("POST", f"/email-drafts/{tenant}/approve", {}),
        ("POST", "/notes", {"body": "hello", "company_id": str(tenant)}),
        ("GET", "/api-keys", None),
        ("POST", "/api-keys", {"name": "More", "scopes": READ}),
        ("GET", "/webhook-endpoints", None),
        ("GET", "/provider-connections", None),
        ("GET", "/audit-events", None),
        ("GET", "/auth/me", None),
        ("POST", "/tenants", {"name": "Mine now"}),
        ("POST", "/invitations", {"email": "x@example.test", "role": "owner"}),
        ("PUT", "/outreach-policy/sender-identity", {"sender_identity": "Someone Else Ltd, Sofia"}),
    ):
        response = reader.request(method, f"{API}{path}", json=body)
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert response.json()["error"]["code"] == "permission_denied"
    assert ok(owner.get(f"{API}/companies"))["total"] == 0


def test_what_a_key_can_hold_is_capped(owner: TestClient, make_client: Any) -> None:
    scopes = {s["name"]: s["grantable"] for s in ok(owner.get(f"{API}/api-keys/scopes"))}
    assert set(scopes) == set(EVERYTHING)
    for forbidden in ("owners.manage", "members.manage", "tenant.settings", "tenant.billing", "integrations.manage",
                      "outreach.approve", "audit.read", "crm.delete", "export.run", "research.review", "calls.log", "everything"):  # fmt: skip
        refused = owner.post(f"{API}/api-keys", json={"name": "Too much", "scopes": ["crm.read", forbidden]})
        assert refused.status_code == 422 and "cannot be given to an API key" in refused.json()["error"]["message"]
    assert owner.post(f"{API}/api-keys", json={"name": "Nothing", "scopes": []}).status_code == 422
    # Only an administrator manages keys.
    manager = make_client()
    sign_in(manager, "manager@example.test")
    join(owner, manager, "manager@example.test", "sales_manager")
    assert manager.post(f"{API}/api-keys", json={"name": "Mine", "scopes": READ}).status_code == 403
    assert manager.get(f"{API}/api-keys").status_code == 403


def test_actions_through_a_key_are_attributed_to_it(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    created = new_key(owner, WRITE)
    agent = with_key(make_client, created["key"])
    company = ok(agent.post(f"{API}/companies", json={"name": "Acme Dental"}), 201)  # no CSRF token, no cookie
    task = ok(agent.post(f"{API}/tasks", json={"title": "Send the brochure", "company_id": company["id"]}), 201)
    assert ok(agent.patch(f"{API}/tasks/{task['id']}", json={"status": "done"}))["status"] == "done"
    ok(agent.post(f"{API}/notes", json={"body": "Asked for the brochure", "company_id": company["id"]}), 201)
    restriction = {"channel_kind": "email", "reason": "Asked for no email"}
    ok(agent.post(f"{API}/companies/{company['id']}/restrictions", json=restriction), 201)
    activities = sql(tenant, "SELECT actor_type, origin FROM activities")
    assert activities and {(a["actor_type"], a["origin"]) for a in activities} == {("integration", "api_key")}
    audited = [e for e in ok(owner.get(f"{API}/audit-events"))["items"] if e["actor_type"] == "integration"]
    assert audited and all(e["data"]["api_key_id"] == created["id"] for e in audited)
    assert ok(owner.get(f"{API}/api-keys"))[0]["last_used_at"] is not None


def test_revoked_expired_and_invalid_keys_are_refused(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    created = new_key(owner, READ)
    agent = with_key(make_client, created["key"])
    assert agent.get(f"{API}/companies").status_code == 200
    for bad in ("crm_" + "A" * 43, created["key"][:-1] + "x", "crm_", "Bearer", created["key"].upper()):
        response = with_key(make_client, bad).get(f"{API}/companies")
        assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated", bad
    assert make_client().get(f"{API}/companies", headers={"Authorization": "Basic abc"}).status_code == 401

    sql(tenant, "UPDATE api_keys SET expires_at = now() - interval '1 second'")
    assert agent.get(f"{API}/companies").status_code == 401
    assert ok(owner.get(f"{API}/api-keys"))[0]["usable"] is False
    sql(tenant, "UPDATE api_keys SET expires_at = now() + interval '1 day'")
    assert agent.get(f"{API}/companies").status_code == 200

    revoked = ok(owner.delete(f"{API}/api-keys/{created['id']}"))
    assert revoked["revoked_at"] is not None and revoked["usable"] is False
    assert agent.get(f"{API}/companies").status_code == 401
    assert ok(owner.delete(f"{API}/api-keys/{created['id']}"))["revoked_at"] == revoked["revoked_at"]  # stays revoked


def test_a_key_follows_its_creators_membership(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    agent = with_key(make_client, new_key(admin, WRITE)["key"])
    assert agent.post(f"{API}/companies", json={"name": "Before"}).status_code == 201
    member = next(m for m in ok(owner.get(f"{API}/members"))["items"] if m["email"] == "admin@example.test")
    ok(owner.patch(f"{API}/members/{member['user_id']}", json={"role": "read_only"}))
    assert (
        agent.post(f"{API}/companies", json={"name": "After demotion"}).status_code == 403
    )  # no more than its creator
    assert agent.get(f"{API}/companies").status_code == 200
    assert owner.delete(f"{API}/members/{member['user_id']}").status_code in (200, 204)
    assert agent.get(f"{API}/companies").status_code == 401  # the creator left: the key stops working


def test_a_key_is_bound_to_its_tenant(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    other = make_client()
    sign_in(other, "other@example.test")
    other_tenant = create_workspace(other, "Another customer")
    theirs = ok(other.post(f"{API}/companies", json={"name": "Their secret customer"}), 201)
    agent = with_key(make_client, new_key(owner, WRITE)["key"])
    # Naming another tenant in a header, a query or a body changes nothing.
    headers = {"X-Tenant-Id": other_tenant, "X-Tenant": other_tenant}
    assert ok(agent.get(f"{API}/companies", headers=headers, params={"tenant_id": other_tenant}))["total"] == 0
    assert agent.get(f"{API}/companies/{theirs['id']}", headers=headers).status_code == 404
    # A tenant named in the body is not accepted at all.
    assert (
        agent.post(f"{API}/companies", json={"name": "Mine", "tenant_id": other_tenant}, headers=headers).status_code
        == 422
    )
    made = ok(agent.post(f"{API}/companies", json={"name": "Mine"}, headers=headers), 201)
    assert sql(tenant, "SELECT count(*) AS n FROM companies WHERE id = :id", id=made["id"])[0]["n"] == 1
    assert ok(other.get(f"{API}/companies"))["total"] == 1 and ok(other.get(f"{API}/api-keys")) == []
    assert other.delete(f"{API}/api-keys/{ok(owner.get(f'{API}/api-keys'))[0]['id']}").status_code == 404


def test_a_key_is_rate_limited(owner: TestClient, make_client: Any) -> None:
    agent = with_key(make_client, new_key(owner, READ, rate_limit_per_minute=3)["key"])
    responses = [agent.get(f"{API}/companies") for _ in range(8)]
    limited = [r for r in responses if r.status_code == 429]
    # Three per minute: at most six can succeed even if the minute turns over mid-test.
    assert len(limited) >= 2 and responses[0].status_code == 200
    assert limited[0].json()["error"]["code"] == "rate_limited" and 1 <= int(limited[0].headers["Retry-After"]) <= 60
    other = with_key(make_client, new_key(owner, READ)["key"])
    assert other.get(f"{API}/companies").status_code == 200  # each key has its own allowance


def test_a_key_with_every_scope_still_cannot_make_a_persons_decisions(
    owner: TestClient, tenant: UUID, make_client: Any
) -> None:
    import_and_commit(
        owner,
        workbook_bytes([lead_row("K-001", "Salon Aurora", **{"Public business email": "office@example-salon.bg"})]),
    )
    lead = ok(owner.get(f"{API}/leads"))["items"][0]
    detail = ok(owner.get(f"{API}/prospects/{lead['id']}"))
    phone = next(c for c in detail["channels"] if c["kind"] == "phone")
    ok(
        owner.patch(
            f"{API}/channels/{phone['id']}",
            json={"do_not_contact": True, "restriction_reason": "Asked not to be called"},
        )
    )
    ok(owner.patch(f"{API}/leads/{lead['id']}", json={"outreach_status": "do_not_contact"}))
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    config = {"name": "Big", "country": "Bulgaria", "cities": ["Sofia"], "categories": ["salon"], "cost_cap": "1000",
              "candidate_cap": 2000, "qualified_target": 2000}  # fmt: skip
    mine = ok(owner.post(f"{API}/research-configs", json={**config, "cost_cap": "1"}), 201)

    agent = with_key(make_client, new_key(owner, EVERYTHING)["key"])
    anything = str(tenant)
    for method, path, body in (
        # approving, and the judgements that make a message eligible
        ("POST", f"/email-drafts/{draft['id']}/approve", {"review_note": "I am sure this is fine"}),
        (
            "PUT",
            "/recipient-profiles",
            {
                "address": "office@example-salon.bg",
                "legal_form": "legal_person",
                "context": "business",
                "evidence": "trust me",
            },
        ),
        (
            "POST",
            "/email-consents",
            {
                "address": "office@example-salon.bg",
                "kind": "opt_in",
                "scope": "everything",
                "source": "an agent says so",
                "obtained_at": "2026-10-09T08:00:00Z",
            },
        ),
        ("POST", f"/email-suppressions/{anything}/lift", {"lift_note": "they changed their mind"}),
        ("POST", "/outreach-policy/approve", {}),
        ("POST", f"/send-intents/{anything}/resolve", {"sent": True, "note": "it definitely went"}),
        ("POST", f"/email-threads/{anything}/link", {"ignore": True}),
        # loosening a contact restriction
        ("PATCH", f"/channels/{phone['id']}", {"do_not_contact": False}),
        ("PATCH", f"/channels/{phone['id']}", {"verification_state": "verified"}),
        ("PATCH", f"/leads/{lead['id']}", {"outreach_status": "not_contacted"}),
        ("PATCH", f"/leads/{lead['id']}", {"status": "qualified"}),
        # research judgements and limits
        ("POST", f"/observations/{anything}/verify", {"state": "verified"}),
        ("POST", f"/research-candidates/{anything}/promote", {}),
        ("POST", f"/leads/{lead['id']}/scores", {}),
        ("POST", f"/prospects/{lead['id']}/dismiss", {"reason": "not a fit"}),
        ("POST", "/research-configs", config),
        ("PUT", f"/research-configs/{mine['id']}", config),
        # saying what happened on a call
        ("POST", f"/prospects/{lead['id']}/calls", {"channel_id": phone["id"]}),
        ("POST", f"/calls/{anything}/outcome", {"outcome": "connected"}),
        # data out in bulk, and deletion
        ("GET", "/exports/prospects.xlsx", None),
        ("DELETE", f"/companies/{lead['company_id']}", None),
    ):
        response = agent.request(method, f"{API}{path}", json=body)
        assert response.status_code == 403, (method, path, response.status_code, response.text[:200])
    after = ok(owner.get(f"{API}/prospects/{lead['id']}"))
    assert next(c for c in after["channels"] if c["id"] == phone["id"])["do_not_contact"] is True
    assert ok(owner.get(f"{API}/leads/{lead['id']}"))["outreach_status"] == "do_not_contact"
    assert ok(owner.get(f"{API}/research-configs"))[0]["cost_cap"] in ("1", "1.0000")
    assert ok(owner.get(f"{API}/email-drafts/{draft['id']}"))["status"] == "draft"
    # What it can do: tighten, prepare, and start work inside limits a person set.
    assert agent.patch(f"{API}/channels/{phone['id']}", json={"label": "Reception"}).status_code == 200
    assert agent.post(f"{API}/email-suppressions", json={"value": "blocked@example.bg"}).status_code == 201
    assert agent.patch(f"{API}/email-drafts/{draft['id']}", json={"subject": "A clearer subject"}).status_code == 200
    assert agent.get(f"{API}/research-runs").status_code == 200


# --- idempotency -----------------------------------------------------------------------------


def test_repeating_a_request_with_the_same_key_does_it_once(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    agent = with_key(make_client, new_key(owner, WRITE)["key"])
    headers = {"Idempotency-Key": "order-1"}
    first = agent.post(f"{API}/companies", json={"name": "Acme Dental"}, headers=headers)
    again = agent.post(f"{API}/companies", json={"name": "Acme Dental"}, headers=headers)
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    assert "idempotency-replayed" not in first.headers and again.headers["idempotency-replayed"] == "true"
    assert ok(owner.get(f"{API}/companies"))["total"] == 1

    for different in (
        agent.post(f"{API}/companies", json={"name": "A different company"}, headers=headers),
        agent.post(f"{API}/tasks", json={"name": "Acme Dental"}, headers=headers),
        agent.post(f"{API}/companies?verbose=1", json={"name": "Acme Dental"}, headers=headers),
    ):
        assert different.status_code == 422 and different.json()["error"]["code"] == "idempotency_conflict"
    assert ok(owner.get(f"{API}/companies"))["total"] == 1

    # A refusal is an answer too, and is repeated rather than re-evaluated.
    bad = {"Idempotency-Key": "order-2"}
    assert agent.post(f"{API}/companies", json={}, headers=bad).status_code == 422
    assert agent.post(f"{API}/companies", json={}, headers=bad).headers["idempotency-replayed"] == "true"
    assert (
        agent.post(f"{API}/companies", json={"name": "Second"}, headers={"Idempotency-Key": "order-3"}).status_code
        == 201
    )
    # The same key string used by another API key is a separate request.
    second = with_key(make_client, new_key(owner, WRITE)["key"])
    assert second.post(f"{API}/companies", json={"name": "Third"}, headers=headers).status_code == 201
    assert ok(owner.get(f"{API}/companies"))["total"] == 3
    assert agent.post(f"{API}/companies", json={"name": "x"}, headers={"Idempotency-Key": "k" * 201}).status_code == 422
    # An invalid key with an idempotency header gets the ordinary 401 and records nothing.
    assert (
        with_key(make_client, "crm_" + "B" * 43)
        .post(f"{API}/companies", json={"name": "x"}, headers=headers)
        .status_code
        == 401
    )

    # "Not now" is not an answer to keep: after a rate limit the same key works.
    slow = with_key(make_client, new_key(owner, WRITE, rate_limit_per_minute=1)["key"])
    assert slow.get(f"{API}/companies").status_code == 200  # uses the minute's allowance
    wait = {"Idempotency-Key": "after-the-limit"}
    assert slow.post(f"{API}/companies", json={"name": "Later"}, headers=wait).status_code == 429
    assert sql(tenant, "SELECT count(*) AS n FROM idempotency_keys WHERE key = 'after-the-limit'")[0]["n"] == 0
    from app.core.deps import get_redis

    for bucket in get_redis().scan_iter("crm:ratelimit:key:*"):
        get_redis().delete(bucket)  # the next minute
    later = slow.post(f"{API}/companies", json={"name": "Later"}, headers=wait)
    assert later.status_code == 201 and "idempotency-replayed" not in later.headers
    assert ok(owner.get(f"{API}/companies"))["total"] == 4

    # A request whose outcome was never recorded is refused, not run again.
    key_id = ok(owner.get(f"{API}/api-keys"))[-1]["id"]
    sql(
        tenant,
        "INSERT INTO idempotency_keys (tenant_id, api_key_id, key, request_hash) VALUES (:t, :k, 'lost', 'whatever')",
        t=tenant,
        k=key_id,
    )
    sql(
        tenant,
        "UPDATE idempotency_keys SET request_hash = (SELECT request_hash FROM idempotency_keys WHERE key = 'order-3') WHERE key = 'lost'",
    )
    lost = agent.post(f"{API}/companies", json={"name": "Second"}, headers={"Idempotency-Key": "lost"})
    assert lost.status_code == 409 and lost.json()["error"]["code"] == "idempotency_in_progress"
    assert ok(owner.get(f"{API}/companies"))["total"] == 4


def test_a_server_error_is_never_followed_by_a_second_execution(
    owner: TestClient, tenant: UUID, make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.crm import companies_router

    company = ok(owner.post(f"{API}/companies", json={"name": "Acme"}), 201)
    agent = with_key(make_client, new_key(owner, WRITE)["key"])
    path, body = (
        f"{API}/companies/{company['id']}/restrictions",
        {"channel_kind": "email", "reason": "Asked for no email"},
    )
    headers = {"Idempotency-Key": "boom"}
    calls: list[int] = []

    def explode(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise RuntimeError("the server broke part-way through")

    monkeypatch.setattr(companies_router, "record_audit", explode)
    with pytest.raises(RuntimeError):  # the test client surfaces what a real client would see as a 500
        agent.post(path, json=body, headers=headers)
    assert len(calls) == 1
    monkeypatch.undo()
    again = agent.post(path, json=body, headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "idempotency_in_progress"
    assert sql(tenant, "SELECT count(*) AS n FROM contact_restrictions")[0]["n"] == 0  # refused, not run again
    assert agent.post(path, json=body, headers={"Idempotency-Key": "boom-after-checking"}).status_code == 201


def test_simultaneous_identical_requests_create_one_record(owner: TestClient, make_client: Any) -> None:
    key = new_key(owner, WRITE)["key"]
    statuses: list[int] = []
    barrier = threading.Barrier(6)

    def fire() -> None:
        client = with_key(make_client, key)
        barrier.wait()
        statuses.append(
            client.post(
                f"{API}/companies", json={"name": "Once only"}, headers={"Idempotency-Key": "burst"}
            ).status_code
        )

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(statuses) <= {201, 409} and statuses.count(201) >= 1, statuses
    assert ok(owner.get(f"{API}/companies"))["total"] == 1


# --- webhooks --------------------------------------------------------------------------------


class Receiver:
    """Stands in for the customer's server."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list[httpx.Response | Exception] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else httpx.Response(200)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def receiver() -> Any:
    target = Receiver()
    webhooks.set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(target.handler)))
    yield target
    webhooks.set_client_factory(None)


def endpoint(owner: TestClient, **body: Any) -> dict[str, Any]:
    return ok(owner.post(f"{API}/webhook-endpoints", json={"url": "https://hooks.customer.example/crm", **body}), 201)  # type: ignore[no-any-return]


def run_deliveries() -> list[str]:
    return [due.run_claimed(job) for job in due.claim_due(50) if job["kind"] == "webhook.deliver"]


def make_due(tenant: UUID) -> None:
    sql(tenant, "UPDATE due_jobs SET due_at = now() WHERE kind = 'webhook.deliver' AND status = 'pending'")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook", "https://127.0.0.1/hook", "https://localhost/hook", "https://10.0.0.5/hook",
        "https://192.168.1.10/hook", "https://169.254.169.254/latest/meta-data", "https://[::1]/hook",
        "https://[::ffff:10.0.0.5]/hook", "https://user:pw@hooks.customer.example/x", "ftp://hooks.customer.example/x",
        "https://db.internal/hook", "https://hooks.customer.example:6379/x", "file:///etc/passwd", "not a url at all",
    ],
)  # fmt: skip
def test_internal_and_unusual_webhook_addresses_are_refused(owner: TestClient, url: str) -> None:
    refused = owner.post(f"{API}/webhook-endpoints", json={"url": url})
    assert refused.status_code == 422, url
    assert ok(owner.get(f"{API}/webhook-endpoints")) == []


def test_plain_http_is_refused_outside_development(owner: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "environment", "production")
    refused = owner.post(f"{API}/webhook-endpoints", json={"url": "http://hooks.customer.example/crm"})
    assert refused.status_code == 422 and "https" in refused.json()["error"]["message"]


def test_events_are_signed_and_the_signature_rejects_tampering(
    owner: TestClient, tenant: UUID, receiver: Receiver
) -> None:
    created = endpoint(owner)
    secret = created["secret"]
    assert secret.startswith("whsec_") and secret not in json.dumps(ok(owner.get(f"{API}/webhook-endpoints")))
    stored = sql(tenant, "SELECT secret_ciphertext FROM webhook_endpoints")[0]
    assert secret.encode() not in bytes(stored["secret_ciphertext"])

    delivery = ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
    assert delivery["status"] == "pending" and receiver.requests == []  # sent by the worker, not by the request
    assert run_deliveries() == ["done"]
    request = receiver.requests[0]
    body, header = request.content, request.headers["X-CRM-Signature"]
    event = json.loads(body)
    assert (
        event["type"] == "webhook.test" and event["api_version"] == "2026-10-01" and event["tenant_id"] == str(tenant)
    )
    assert event["id"] == delivery["event_id"] == request.headers["X-CRM-Event-Id"]
    assert (
        request.headers["X-CRM-Delivery-Id"] == delivery["id"]
        and str(request.url) == "https://hooks.customer.example/crm"
    )

    assert webhooks.verify(secret, header, body) is True
    assert webhooks.verify(secret, header, body.replace(b"test event", b"real event")) is False
    assert webhooks.verify("whsec_" + "x" * 43, header, body) is False
    assert webhooks.verify(secret, header.replace("v1=", "v1=0"), body) is False
    assert webhooks.verify(secret, "v1=" + header.split("v1=")[1], body) is False  # no timestamp
    # An old captured delivery cannot be replayed at the receiver later.
    assert webhooks.verify(secret, header, body, now=int(time.time()) + 3600) is False
    stamp = int(header.split(",")[0][2:])
    forged = f"t={stamp + 1},v1={header.split('v1=')[1]}"
    assert webhooks.verify(secret, forged, body) is False  # the timestamp is part of what is signed
    listed = ok(owner.get(f"{API}/webhook-deliveries"))["items"][0]
    assert (listed["status"], listed["attempts"], listed["last_status_code"]) == ("delivered", 1, 200)


def test_an_event_exists_only_if_its_transaction_commits(owner: TestClient, tenant: UUID, receiver: Receiver) -> None:
    endpoint(owner, event_types=["call.outcome_reported"])
    endpoint(owner, url="https://other.customer.example/all")
    paused = endpoint(owner, url="https://paused.customer.example/x")
    ok(owner.patch(f"{API}/webhook-endpoints/{paused['id']}", json={"status": "paused"}))

    with pytest.raises(RuntimeError), session_scope(RlsContext(tenant_id=tenant)) as db:
        outbox.emit(db, tenant, "call.outcome_reported", subject_type="call_attempt", subject_id=None, payload={"x": 1})
        raise RuntimeError("the business change failed")
    assert sql(tenant, "SELECT count(*) AS n FROM outbox_events")[0]["n"] == 0
    assert sql(tenant, "SELECT count(*) AS n FROM webhook_deliveries")[0]["n"] == 0 and run_deliveries() == []

    # A real change: a call outcome reported through the API.
    import_and_commit(owner, workbook_bytes([lead_row("W-001", "Salon Aurora")]))
    lead = ok(owner.get(f"{API}/leads"))["items"][0]
    phone = next(c for c in ok(owner.get(f"{API}/prospects/{lead['id']}"))["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{lead['id']}/calls", json={"channel_id": phone["id"]}), 201)
    ok(owner.post(f"{API}/calls/{call['id']}/outcome", json={"outcome": "no_answer"}))
    assert sorted(run_deliveries()) == ["done", "done"]  # the filtered endpoint and the catch-all; not the paused one
    assert sorted(str(r.url) for r in receiver.requests) == [
        "https://hooks.customer.example/crm",
        "https://other.customer.example/all",
    ]
    events = [json.loads(r.content) for r in receiver.requests]
    assert {e["type"] for e in events} == {"call.outcome_reported"} and len({e["id"] for e in events}) == 1
    assert events[0]["data"]["outcome"] == "no_answer" and events[0]["subject"] == {
        "type": "call_attempt",
        "id": call["id"],
    }

    # An event the filtered endpoint did not ask for goes only to the catch-all.
    with session_scope(RlsContext(tenant_id=tenant)) as db:
        outbox.emit(db, tenant, "research_run.finished", subject_type="research_run", subject_id=None, payload={})
    assert run_deliveries() == ["done"] and str(receiver.requests[-1].url) == "https://other.customer.example/all"
    assert (
        owner.post(
            f"{API}/webhook-endpoints", json={"url": "https://x.example/h", "event_types": ["made.up"]}
        ).status_code
        == 422
    )


def test_failed_deliveries_back_off_die_and_can_be_replayed_with_the_same_event_id(
    owner: TestClient, tenant: UUID, receiver: Receiver
) -> None:
    created = endpoint(owner)
    delivery = ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
    receiver.answers = [
        httpx.Response(500),
        httpx.ReadTimeout("slow"),
        httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}),
        httpx.ConnectError("refused"),
        httpx.Response(404),
        httpx.Response(503),
        httpx.Response(500),
    ]
    waits, errors = [], []
    for _ in range(7):
        before = datetime.now(UTC)
        make_due(tenant)
        outcome = run_deliveries()
        row = sql(tenant, "SELECT status, attempts, next_attempt_at, last_error FROM webhook_deliveries")[0]
        waits.append(round((row["next_attempt_at"] - before).total_seconds() / 60) if row["next_attempt_at"] else None)
        errors.append(row["last_error"])
        assert outcome == (["rescheduled"] if row["status"] == "pending" else ["done"])
    assert waits == [1, 5, 30, 120, 360, 1440, None]  # the schedule is in the database, and it ends
    assert "answered 500" in errors[0] and "did not answer in time" in errors[1] and "answered 302" in errors[2]
    assert "could not be reached" in errors[3]
    # The redirect was not followed: one request per attempt, all to the registered address.
    assert len(receiver.requests) == 7 and {str(r.url) for r in receiver.requests} == {
        "https://hooks.customer.example/crm"
    }
    dead = ok(owner.get(f"{API}/webhook-deliveries", params={"status": "dead"}))["items"]
    assert [(d["id"], d["attempts"]) for d in dead] == [(delivery["id"], 7)]
    state = ok(owner.get(f"{API}/webhook-endpoints"))[0]
    assert state["consecutive_failures"] == 7 and "answered 500" in state["last_error"]
    make_due(tenant)
    assert run_deliveries() == []  # dead means no more automatic attempts

    replayed = ok(owner.post(f"{API}/webhook-deliveries/{delivery['id']}/replay"))
    assert replayed["status"] == "pending" and replayed["attempts"] == 0
    assert owner.post(f"{API}/webhook-deliveries/{delivery['id']}/replay").status_code == 409
    assert run_deliveries() == ["done"]
    # Same event, same ID: a receiver that remembers IDs does the work once. Nothing was duplicated here either.
    assert {r.headers["X-CRM-Event-Id"] for r in receiver.requests} == {delivery["event_id"]}
    assert sql(tenant, "SELECT count(*) AS n FROM outbox_events")[0]["n"] == 1
    assert sql(tenant, "SELECT count(*) AS n FROM webhook_deliveries")[0]["n"] == 1
    assert ok(owner.get(f"{API}/webhook-endpoints"))[0]["consecutive_failures"] == 0


def test_a_name_that_resolves_to_an_internal_address_is_never_connected_to(owner: TestClient, tenant: UUID) -> None:
    created = endpoint(owner, url="https://rebind.customer.example/hook")
    lookups: list[str] = []

    def resolver(host: str, port: int) -> list[str]:
        lookups.append(host)
        return ["10.0.0.5"]  # public-looking name, internal answer

    webhooks.set_client_factory(lambda: guarded_client(resolver))
    try:
        ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
        assert run_deliveries() == ["rescheduled"]
    finally:
        webhooks.set_client_factory(None)
    row = sql(tenant, "SELECT status, attempts, last_error, last_status_code FROM webhook_deliveries")[0]
    assert lookups == ["rebind.customer.example"] and row["attempts"] == 1 and row["last_status_code"] is None
    assert "not allowed" in row["last_error"] or "could not be reached" in row["last_error"]


def test_rotating_a_secret_keeps_the_old_one_valid_for_a_day(
    owner: TestClient, tenant: UUID, receiver: Receiver
) -> None:
    created = endpoint(owner)
    rotated = ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/rotate-secret"))
    assert rotated["secret"] != created["secret"] and rotated["old_signature_until"] is not None
    ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
    run_deliveries()
    request = receiver.requests[-1]
    assert request.headers["X-CRM-Signature"].count("v1=") == 2
    assert webhooks.verify(created["secret"], request.headers["X-CRM-Signature"], request.content)
    assert webhooks.verify(rotated["secret"], request.headers["X-CRM-Signature"], request.content)

    sql(tenant, "UPDATE webhook_endpoints SET previous_secret_expires_at = now() - interval '1 minute'")
    ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
    run_deliveries()
    request = receiver.requests[-1]
    assert request.headers["X-CRM-Signature"].count("v1=") == 1
    assert not webhooks.verify(created["secret"], request.headers["X-CRM-Signature"], request.content)
    assert webhooks.verify(rotated["secret"], request.headers["X-CRM-Signature"], request.content)


def test_webhooks_belong_to_one_tenant_and_to_administrators(
    owner: TestClient, tenant: UUID, make_client: Any, receiver: Receiver
) -> None:
    created = endpoint(owner)
    delivery = ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
    other = make_client()
    sign_in(other, "other@example.test")
    other_tenant = UUID(create_workspace(other, "Another customer"))
    assert ok(other.get(f"{API}/webhook-endpoints")) == [] and ok(other.get(f"{API}/webhook-deliveries"))["total"] == 0
    for method, path in (
        ("PATCH", f"/webhook-endpoints/{created['id']}"),
        ("DELETE", f"/webhook-endpoints/{created['id']}"),
        ("POST", f"/webhook-endpoints/{created['id']}/rotate-secret"),
        ("POST", f"/webhook-endpoints/{created['id']}/test"),
        ("POST", f"/webhook-deliveries/{delivery['id']}/replay"),
    ):
        assert other.request(method, f"{API}{path}", json={}).status_code == 404, path
    # An event in the other tenant reaches none of this tenant's endpoints.
    with session_scope(RlsContext(tenant_id=other_tenant)) as db:
        outbox.emit(db, other_tenant, "research_run.finished", subject_type="research_run", subject_id=None, payload={})
    assert sql(other_tenant, "SELECT count(*) AS n FROM webhook_deliveries")[0]["n"] == 0
    assert (
        webhooks.deliver(other_tenant, UUID(delivery["id"])) is None and receiver.requests == []
    )  # wrong tenant: invisible

    manager = make_client()
    sign_in(manager, "manager@example.test")
    join(owner, manager, "manager@example.test", "sales_manager")
    assert manager.get(f"{API}/webhook-endpoints").status_code == 403
    assert owner.delete(f"{API}/webhook-endpoints/{created['id']}").status_code == 204
    assert run_deliveries() == ["done"] and receiver.requests == []  # the endpoint is gone; nothing is sent


def test_secret_rotation_covers_webhook_secrets(
    owner: TestClient, tenant: UUID, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from app.core import secrets as secrets_module
    from app.core.secrets import Keyring
    from app.modules.providers import rotate
    from tests.test_providers import key

    created = endpoint(owner)
    ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/rotate-secret"))
    rotated = Keyring(f"next3:{key(b'w')},{os.environ['SECRET_ENCRYPTION_KEYS']}")
    for module in (secrets_module, rotate, webhooks):
        monkeypatch.setattr(module, "get_keyring", lambda: rotated)
    assert rotate.reseal_all() == {"resealed": 2, "already_current": 0, "unreadable": 0}  # current and previous secret
    only_new = Keyring(f"next3:{key(b'w')}")
    for module in (secrets_module, rotate, webhooks):
        monkeypatch.setattr(module, "get_keyring", lambda: only_new)
    ok(owner.post(f"{API}/webhook-endpoints/{created['id']}/test"))
    assert run_deliveries() == ["done"]
    assert webhooks.verify(
        created["secret"], receiver.requests[0].headers["X-CRM-Signature"], receiver.requests[0].content
    )
