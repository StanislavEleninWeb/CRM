"""Provider credentials, health, budgets and usage."""

import base64
import logging
import os
import threading
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import RlsContext, session_scope
from app.core.logging import configure_logging, get_logger
from app.core.secrets import Keyring, Sealed, SecretError, hint
from app.modules.providers import budgets
from app.modules.providers.adapters import available_adapters
from tests.helpers import API, create_workspace, join, sign_in

API_KEY = "sk-live-9fQ2mXv7Lr4TzKc81BnWpYd3"  # looks real; exists only in this test


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@pytest.fixture
def owner(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, "owner@example.test")
    create_workspace(client, "SEWEB")
    return client


@pytest.fixture
def other(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, "other@example.test")
    create_workspace(client, "SEWEB")
    return client


def tenant_of(client: TestClient) -> UUID:
    return UUID(ok(client.get(f"{API}/tenant"))["id"])


def connect(client: TestClient, credential: str = API_KEY, provider: str = "fake_model") -> dict[str, Any]:
    body = {"provider": provider, "label": "Research model", "access_mode": "byok", "credential": credential}
    return ok(client.post(f"{API}/provider-connections", json=body), 201)  # type: ignore[no-any-return]


def key(seed: bytes) -> str:
    return base64.b64encode(seed * 32).decode()


# --- encryption ------------------------------------------------------------------------------


def test_secret_round_trip_is_bound_to_tenant_provider_and_connection() -> None:
    ring = Keyring(f"v1:{key(b'a')}")
    tenant, connection = uuid4(), uuid4()
    sealed = ring.seal(API_KEY, tenant_id=tenant, provider="acme", connection_id=connection)
    assert API_KEY.encode() not in sealed.ciphertext and len(sealed.nonce) == 12 and sealed.key_version == "v1"
    assert ring.open(sealed, tenant_id=tenant, provider="acme", connection_id=connection) == API_KEY
    for wrong in (
        {"tenant_id": uuid4(), "provider": "acme", "connection_id": connection},
        {"tenant_id": tenant, "provider": "other", "connection_id": connection},
        {"tenant_id": tenant, "provider": "acme", "connection_id": uuid4()},
    ):
        with pytest.raises(SecretError):
            ring.open(sealed, **wrong)  # type: ignore[arg-type]
    tampered = Sealed(
        ciphertext=sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1]), nonce=sealed.nonce, key_version="v1"
    )
    with pytest.raises(SecretError):
        ring.open(tampered, tenant_id=tenant, provider="acme", connection_id=connection)
    # The same secret sealed twice never produces the same bytes.
    assert (
        ring.seal(API_KEY, tenant_id=tenant, provider="acme", connection_id=connection).ciphertext != sealed.ciphertext
    )


def test_key_rotation_keeps_old_secrets_readable_until_resealed() -> None:
    tenant, connection = uuid4(), uuid4()
    scope = {"tenant_id": tenant, "provider": "acme", "connection_id": connection}
    old = Keyring(f"v1:{key(b'a')}").seal(API_KEY, **scope)
    rotated = Keyring(f"v2:{key(b'b')},v1:{key(b'a')}")
    assert rotated.current_version == "v2" and rotated.open(old, **scope) == API_KEY
    resealed = rotated.seal(rotated.open(old, **scope), **scope)
    assert resealed.key_version == "v2"
    only_new = Keyring(f"v2:{key(b'b')}")
    assert only_new.open(resealed, **scope) == API_KEY
    with pytest.raises(SecretError, match="not available"):
        only_new.open(old, **scope)
    for bad in ("v1:not-base64!", f"v1:{base64.b64encode(b'short').decode()}", f"v1:{key(b'a')},v1:{key(b'b')}"):
        with pytest.raises(SecretError):
            Keyring(bad)
    with pytest.raises(SecretError, match="no encryption key"):
        Keyring("").seal("x", **scope)
    assert hint(API_KEY) == "…pYd3" and hint("short") == "…"


def test_a_ciphertext_copied_into_another_tenants_row_cannot_be_read(
    owner: TestClient, other: TestClient, migrator_engine: Any
) -> None:
    mine, theirs = connect(owner), connect(other, credential="sk-live-THEIR-OWN-KEY-000000000")
    with migrator_engine.begin() as conn:  # an attacker with database write access swaps the stored secrets
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_of(owner))})
        stolen = conn.execute(
            text("SELECT secret_ciphertext, secret_nonce, secret_key_version FROM provider_connections")
        ).one()
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_of(other))})
        conn.execute(
            text("UPDATE provider_connections SET secret_ciphertext = :c, secret_nonce = :n, secret_key_version = :k"),
            {"c": stolen.secret_ciphertext, "n": stolen.secret_nonce, "k": stolen.secret_key_version},
        )
    refused = other.post(f"{API}/provider-connections/{theirs['id']}/check")
    assert refused.status_code == 409 and "cannot be read" in refused.json()["error"]["message"]
    assert ok(owner.post(f"{API}/provider-connections/{mine['id']}/check"))["status"] == "active"


# --- credentials never leave the server ------------------------------------------------------


def test_credentials_are_write_only(
    owner: TestClient, migrator_engine: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    logging.getLogger().handlers[0].stream = __import__("sys").stdout  # type: ignore[attr-defined]
    created = connect(owner)
    get_logger("test").info(
        "provider_call", authorization=f"Bearer {API_KEY}", api_key=API_KEY, url=f"https://x.test/?key={API_KEY}"
    )
    assert (
        created["has_credential"] is True and created["credential_hint"] == "…pYd3" and created["status"] == "pending"
    )
    checked = ok(owner.post(f"{API}/provider-connections/{created['id']}/check"))
    assert (
        checked["status"] == "active"
        and checked["verification"] == "verified_locally"
        and checked["is_local_test_adapter"] is True
    )
    replaced = ok(
        owner.put(f"{API}/provider-connections/{created['id']}/credential", json={"credential": API_KEY + "-v2"})
    )

    responses = [
        created,
        checked,
        replaced,
        ok(owner.get(f"{API}/provider-connections")),
        ok(owner.get(f"{API}/audit-events")),
    ]
    for body in responses:
        assert API_KEY not in str(body) and "ciphertext" not in str(body) and "nonce" not in str(body)
    with migrator_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_of(owner))})
        stored = conn.execute(text("SELECT secret_ciphertext, secret_hint FROM provider_connections")).one()
    assert API_KEY.encode() not in bytes(stored.secret_ciphertext) and stored.secret_hint == "…3-v2"
    logged = capsys.readouterr().out
    assert "provider_call" in logged and API_KEY not in logged  # structured logs redact it too
    assert (
        owner.post(
            f"{API}/provider-connections", json={"provider": "fake_model", "label": "x", "access_mode": "byok"}
        ).status_code
        == 422
    )
    assert (
        owner.post(
            f"{API}/provider-connections", json={"provider": "nonexistent", "label": "x", "credential": API_KEY}
        ).status_code
        == 422
    )


def test_no_read_schema_exposes_a_secret_field(client: TestClient) -> None:
    schema = client.get(f"{API}/openapi.json").json()
    # Inputs, and the two "shown once at creation" responses.
    write_only = {"ConnectionIn", "CredentialIn", "InvitationAccept", "InvitationCreated", "EndpointCreated"}
    suspicious = ("secret", "ciphertext", "nonce", "password", "token_hash", "credential", "api_key", "private_key")
    allowed = {"has_credential", "credential_hint", "csrf_token", "api_keys"}  # api_keys: a count in a plan
    offenders = []
    for name, model in schema["components"]["schemas"].items():
        if name in write_only:
            continue
        for prop in model.get("properties", {}):
            if any(word in prop.lower() for word in suspicious) and prop not in allowed:
                offenders.append(f"{name}.{prop}")
    assert offenders == []
    assert "credential" in schema["components"]["schemas"]["ConnectionIn"]["properties"]


def test_health_failures_back_off_and_revocation_is_visible(owner: TestClient) -> None:
    throttled = connect(owner, credential="key-that-will-throttle-00000")
    state = ok(owner.post(f"{API}/provider-connections/{throttled['id']}/check"))
    assert state["status"] == "error" and state["rate_limited_until"] and state["consecutive_failures"] == 1
    assert state["last_error"] == "The provider is rate limiting requests."
    broken = connect(owner, credential="key-that-is-broken-000000000")
    first = ok(owner.post(f"{API}/provider-connections/{broken['id']}/check"))
    second = ok(owner.post(f"{API}/provider-connections/{broken['id']}/check"))
    assert second["consecutive_failures"] == 2 and second["rate_limited_until"] > first["rate_limited_until"]

    rejected = connect(owner, credential="key-that-was-revoked-0000000")
    state = ok(owner.post(f"{API}/provider-connections/{rejected['id']}/check"))
    assert state["status"] == "revoked" and state["revoked_at"] is not None
    assert (
        owner.post(f"{API}/provider-connections/{rejected['id']}/check").status_code == 409
    )  # fails visibly, not silently
    # Reconnecting keeps the same record (and so its history) and clears the failure state.
    again = ok(owner.put(f"{API}/provider-connections/{rejected['id']}/credential", json={"credential": API_KEY}))
    assert again["id"] == rejected["id"] and again["status"] == "pending" and again["consecutive_failures"] == 0
    assert ok(owner.post(f"{API}/provider-connections/{rejected['id']}/check"))["status"] == "active"
    erased = ok(owner.delete(f"{API}/provider-connections/{rejected['id']}"))
    assert erased["status"] == "revoked" and erased["has_credential"] is False and erased["credential_hint"] is None


def test_connections_need_permission_and_stay_inside_the_tenant(
    owner: TestClient, other: TestClient, make_client: Any
) -> None:
    mine = connect(owner)
    manager = make_client()
    sign_in(manager, "manager@example.test")
    join(owner, manager, "manager@example.test", "sales_manager")
    assert manager.get(f"{API}/provider-connections").status_code == 403
    assert (
        manager.post(
            f"{API}/provider-connections", json={"provider": "fake_model", "label": "x", "credential": API_KEY}
        ).status_code
        == 403
    )
    assert manager.put(f"{API}/budgets", json={"limit_amount": "10"}).status_code == 403
    assert manager.get(f"{API}/budgets").status_code == 200  # managers may read spend
    assert ok(other.get(f"{API}/provider-connections")) == []
    for method, path, body in (
        ("POST", f"/provider-connections/{mine['id']}/check", None),
        ("PUT", f"/provider-connections/{mine['id']}/credential", {"credential": API_KEY}),
        ("POST", f"/provider-connections/{mine['id']}/reseal", None),
        ("DELETE", f"/provider-connections/{mine['id']}", None),
    ):
        assert other.request(method, f"{API}{path}", json=body).status_code == 404, path
    assert ok(owner.get(f"{API}/provider-connections"))[0]["status"] == "pending"


def test_local_test_adapters_are_refused_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.providers import adapters

    assert {a.name for a in available_adapters()} >= {"fake_model", "fake_discovery"}
    production = Settings(
        environment="production",
        database_url="postgresql+psycopg://crm_app:real@db/crm",
        session_secret="s" * 40,
        public_base_url="https://crm.example.test",
        oidc_issuer="https://idp.example.test",
        oidc_client_secret="real",
        oidc_dev_provider=False,
        s3_secret_key="real",
        erasure_hash_key="a-real-erasure-key-of-thirty-two-chars-or-more",
    )
    monkeypatch.setattr(adapters, "get_settings", lambda: production)
    assert [a.name for a in available_adapters() if a.is_fake] == []
    assert adapters.get_adapter("fake_model") is None
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://a:b@db/crm",
            session_secret="s" * 40,
            public_base_url="https://x.test",
            oidc_issuer="https://i.test",
            oidc_client_secret="change-me-oidc",
        )


# --- budgets ---------------------------------------------------------------------------------


def budget(client: TestClient, limit: str = "100", **extra: Any) -> dict[str, Any]:
    return ok(
        client.put(f"{API}/budgets", json={"scope": "research", "period": "month", "limit_amount": limit, **extra})
    )  # type: ignore[no-any-return]


def in_tenant(tenant_id: UUID) -> Any:
    return session_scope(RlsContext(tenant_id=tenant_id))


def test_paid_work_is_refused_until_a_budget_exists(owner: TestClient) -> None:
    tenant = tenant_of(owner)
    with pytest.raises(budgets.NoBudget), in_tenant(tenant) as db:
        budgets.reserve(
            db, tenant, scope="research", amount=Decimal("1"), idempotency_key="run-1:step-1", purpose="research"
        )
    created = budget(owner, "50")
    assert (created["currency"], created["available_amount"], created["allow_overage"]) == ("EUR", "50.0000", False)
    with in_tenant(tenant) as db:
        held = budgets.reserve(
            db, tenant, scope="research", amount=Decimal("20"), idempotency_key="run-1:step-1", purpose="research"
        )
    assert held.created and held.state == "reserved"
    assert ok(owner.get(f"{API}/budgets"))[0]["available_amount"] == "30.0000"
    assert (
        owner.put(f"{API}/budgets", json={"scope": "research", "limit_amount": "10"}).status_code == 409
    )  # below what is held


def test_concurrent_jobs_cannot_oversubscribe_a_budget(owner: TestClient) -> None:
    """Twenty real threads, each on its own database connection, race for a budget that fits ten."""
    tenant = tenant_of(owner)
    budget(owner, "100", max_concurrent_runs=50)
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=20, max_overflow=0)
    barrier = threading.Barrier(20)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        with Session(engine) as db:
            db.info["rls"] = RlsContext(tenant_id=tenant)
            barrier.wait()
            try:
                budgets.reserve(
                    db,
                    tenant,
                    scope="research",
                    amount=Decimal("10"),
                    idempotency_key=f"race-run-{n}:step",
                    purpose="research",
                    run_ref=f"run-{n}",
                )
                db.commit()
                result = "reserved"
            except budgets.BudgetExceeded:
                db.rollback()
                result = "refused"
            with lock:
                outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    engine.dispose()
    assert sorted(outcomes) == ["refused"] * 10 + ["reserved"] * 10
    state = ok(owner.get(f"{API}/budgets"))[0]
    assert (state["reserved_amount"], state["spent_amount"], state["available_amount"]) == (
        "100.0000",
        "0.0000",
        "0.0000",
    )
    assert ok(owner.get(f"{API}/budget-reservations", params={"state": "reserved"}))["total"] == 10


def test_a_retry_with_the_same_key_never_reserves_twice(owner: TestClient) -> None:
    tenant = tenant_of(owner)
    budget(owner, "100", max_concurrent_runs=50)
    with in_tenant(tenant) as db:
        first = budgets.reserve(
            db, tenant, scope="research", amount=Decimal("40"), idempotency_key="run-7:fetch", purpose="research"
        )
    with in_tenant(tenant) as db:
        retry = budgets.reserve(
            db, tenant, scope="research", amount=Decimal("40"), idempotency_key="run-7:fetch", purpose="research"
        )
    assert retry.id == first.id and first.created and not retry.created
    assert ok(owner.get(f"{API}/budgets"))[0]["reserved_amount"] == "40.0000"

    # The same key from eight threads at once still yields one reservation.
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=8, max_overflow=0)
    barrier, ids = threading.Barrier(8), []

    def worker() -> None:
        with Session(engine) as db:
            db.info["rls"] = RlsContext(tenant_id=tenant)
            barrier.wait()
            reservation = budgets.reserve(
                db, tenant, scope="research", amount=Decimal("30"), idempotency_key="run-8:fetch", purpose="research"
            )
            db.commit()
            ids.append(reservation.id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    engine.dispose()
    assert len(ids) == 8 and len(set(ids)) == 1
    assert ok(owner.get(f"{API}/budgets"))[0]["reserved_amount"] == "70.0000"
    assert ok(owner.get(f"{API}/budget-reservations"))["total"] == 2


def test_settling_releases_the_unused_part_and_records_the_cost_basis(owner: TestClient) -> None:
    tenant = tenant_of(owner)
    connection = connect(owner)
    budget(owner, "100")
    with in_tenant(tenant) as db:
        held = budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("25"),
            idempotency_key="run-1:model",
            purpose="research",
            connection_id=UUID(connection["id"]),
            run_ref="run-1",
        )
        budgets.settle(
            db,
            tenant,
            held.id,
            actual=Decimal("7.25"),
            cost_basis="estimated",
            provider="fake_model",
            units={"input_tokens": 1200, "output_tokens": 300},
            billed_to="tenant_provider_account",
            pricing_version="2026-10",
        )
        again = budgets.settle(db, tenant, held.id, actual=Decimal("99"), cost_basis="estimated", provider="fake_model")
    assert again.state == "settled" and again.actual_amount == Decimal("7.2500")  # settling twice changes nothing
    state = ok(owner.get(f"{API}/budgets"))[0]
    assert (state["spent_amount"], state["reserved_amount"], state["available_amount"]) == (
        "7.2500",
        "0.0000",
        "92.7500",
    )
    usage = ok(owner.get(f"{API}/usage/summary"))
    assert (usage["estimated_amount"], usage["verified_amount"], usage["entries"]) == ("7.2500", "0.0000", 1)
    assert usage["by_provider"] == [
        {
            "provider": "fake_model",
            "cost_basis": "estimated",
            "billed_to": "tenant_provider_account",
            "amount": "7.2500",
            "entries": 1,
        }
    ]
    with in_tenant(tenant) as db:
        other = budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("5"),
            idempotency_key="run-1:places",
            purpose="research",
            run_ref="run-1",
        )
        budgets.settle(
            db, tenant, other.id, actual=Decimal("5"), cost_basis="provider_reported", provider="fake_discovery"
        )
    usage = ok(owner.get(f"{API}/usage/summary"))
    assert (usage["estimated_amount"], usage["verified_amount"]) == ("7.2500", "5.0000")  # never added together


def test_a_timeout_keeps_the_budget_held_until_a_person_resolves_it(owner: TestClient) -> None:
    tenant = tenant_of(owner)
    budget(owner, "100")
    with in_tenant(tenant) as db:
        held = budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("60"),
            idempotency_key="run-1:call",
            purpose="research",
            run_ref="run-1",
        )
        unknown = budgets.mark_unknown(db, tenant, held.id, note="Provider timed out after 30 s")
    assert unknown.state == "unknown"
    assert ok(owner.get(f"{API}/budgets"))[0]["available_amount"] == "40.0000"  # still held
    with pytest.raises(Exception, match="resolved by a person"), in_tenant(tenant) as db:
        budgets.release(db, tenant, held.id)
    with pytest.raises(budgets.BudgetExceeded), in_tenant(tenant) as db:
        budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("50"),
            idempotency_key="run-1:next",
            purpose="research",
            run_ref="run-1",
        )
    summary = ok(owner.get(f"{API}/usage/summary"))
    assert (summary["unknown_reserved_amount"], summary["reserved_amount"]) == ("60.0000", "0.0000")

    assert (
        owner.post(
            f"{API}/budget-reservations/{held.id}/resolve", json={"charged": True, "note": "Checked invoice"}
        ).status_code
        == 422
    )
    resolved = ok(
        owner.post(
            f"{API}/budget-reservations/{held.id}/resolve",
            json={"charged": True, "actual_amount": "12.5", "note": "Provider dashboard shows the call was billed"},
        )
    )
    assert (resolved["state"], resolved["actual_amount"]) == ("settled", "12.5000")
    assert ok(owner.get(f"{API}/budgets"))[0]["available_amount"] == "87.5000"
    assert (
        owner.post(f"{API}/budget-reservations/{held.id}/resolve", json={"charged": False, "note": "again"}).status_code
        == 409
    )

    with in_tenant(tenant) as db:
        second = budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("10"),
            idempotency_key="run-2:call",
            purpose="research",
            run_ref="run-2",
        )
        budgets.mark_unknown(db, tenant, second.id, note="Connection reset")
    not_charged = ok(
        owner.post(
            f"{API}/budget-reservations/{second.id}/resolve",
            json={"charged": False, "note": "No charge on the provider side"},
        )
    )
    assert not_charged["state"] == "released"
    assert ok(owner.get(f"{API}/budgets"))[0]["available_amount"] == "87.5000"
    assert "budget.reservation_resolved" in [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]


def test_a_real_charge_above_the_reservation_stops_further_work(owner: TestClient) -> None:
    tenant = tenant_of(owner)
    budget(owner, "20")
    with in_tenant(tenant) as db:
        held = budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("15"),
            idempotency_key="run-1:a",
            purpose="research",
            run_ref="run-1",
        )
        budgets.settle(db, tenant, held.id, actual=Decimal("26"), cost_basis="provider_reported", provider="fake_model")
    state = ok(owner.get(f"{API}/budgets"))[0]
    assert (state["spent_amount"], state["overrun"], state["available_amount"]) == (
        "26.0000",
        True,
        "0.0000",
    )  # the truth is recorded
    with pytest.raises(budgets.BudgetExceeded), in_tenant(tenant) as db:
        budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("0.01"),
            idempotency_key="run-1:b",
            purpose="research",
            run_ref="run-1",
        )
    raised = budget(owner, "60")
    assert raised["overrun"] is False and raised["available_amount"] == "34.0000"


def test_concurrent_run_limit_and_release(owner: TestClient) -> None:
    tenant = tenant_of(owner)
    budget(owner, "100", max_concurrent_runs=2)
    with in_tenant(tenant) as db:
        a = budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("1"),
            idempotency_key="run-a:1",
            purpose="research",
            run_ref="run-a",
        )
        budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("1"),
            idempotency_key="run-a:2",
            purpose="research",
            run_ref="run-a",
        )
        budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("1"),
            idempotency_key="run-b:1",
            purpose="research",
            run_ref="run-b",
        )
    with pytest.raises(budgets.BudgetExceeded, match="2 run"), in_tenant(tenant) as db:
        budgets.reserve(
            db,
            tenant,
            scope="research",
            amount=Decimal("1"),
            idempotency_key="run-c:1",
            purpose="research",
            run_ref="run-c",
        )
    with in_tenant(tenant) as db:
        released = budgets.release(db, tenant, a.id, note="Step skipped")
        assert released.state == "released" and budgets.release(db, tenant, a.id).state == "released"
    assert ok(owner.get(f"{API}/budgets"))[0]["reserved_amount"] == "2.0000"


def test_budgets_are_isolated_and_the_ledger_is_append_only(owner: TestClient, other: TestClient) -> None:
    tenant_a, tenant_b = tenant_of(owner), tenant_of(other)
    budget(owner, "100")
    with in_tenant(tenant_a) as db:
        held = budgets.reserve(
            db, tenant_a, scope="research", amount=Decimal("5"), idempotency_key="shared-key-1", purpose="research"
        )
        budgets.settle(db, tenant_a, held.id, actual=Decimal("5"), cost_basis="estimated", provider="fake_model")
    assert ok(other.get(f"{API}/budgets")) == [] and ok(other.get(f"{API}/usage/summary"))["entries"] == 0
    assert ok(other.get(f"{API}/budget-reservations"))["total"] == 0
    assert (
        other.post(f"{API}/budget-reservations/{held.id}/resolve", json={"charged": False, "note": "nope"}).status_code
        == 409
    )
    with pytest.raises(budgets.NoBudget), in_tenant(tenant_b) as db:  # tenant B has no budget of its own
        budgets.reserve(
            db, tenant_b, scope="research", amount=Decimal("1"), idempotency_key="shared-key-1", purpose="research"
        )
    with pytest.raises(Exception, match="permission denied"), in_tenant(tenant_a) as db:
        db.execute(text("UPDATE usage_ledger SET amount = 0"))
    with pytest.raises(Exception, match="permission denied"), in_tenant(tenant_a) as db:
        db.execute(text("DELETE FROM usage_ledger"))


def test_rotation_script_reseals_every_tenants_credentials(
    owner: TestClient, other: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import secrets as secrets_module
    from app.modules.providers import rotate, router

    mine, theirs = connect(owner), connect(other, credential="sk-live-THEIR-OWN-KEY-000000000")
    old_spec = os.environ["SECRET_ENCRYPTION_KEYS"]
    rotated = Keyring(f"next2:{key(b'n')},{old_spec}")
    for module in (secrets_module, rotate, router):
        monkeypatch.setattr(module, "get_keyring", lambda: rotated)
    assert rotate.reseal_all() == {"resealed": 2, "already_current": 0, "unreadable": 0}
    assert rotate.reseal_all() == {"resealed": 0, "already_current": 2, "unreadable": 0}
    assert ok(owner.get(f"{API}/provider-connections"))[0]["encryption_key_version"] == "next2"

    only_new = Keyring(f"next2:{key(b'n')}")  # the old key is retired
    for module in (secrets_module, rotate, router):
        monkeypatch.setattr(module, "get_keyring", lambda: only_new)
    assert ok(owner.post(f"{API}/provider-connections/{mine['id']}/check"))["status"] == "active"
    assert ok(other.post(f"{API}/provider-connections/{theirs['id']}/check"))["status"] == "active"
