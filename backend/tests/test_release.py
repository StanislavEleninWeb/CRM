# ruff: noqa: F811
"""Release evidence: one journey across the whole product with two workspaces, and isolation checked from outside.

Everything runs as the runtime database role. Providers are local stand-ins: no email leaves the machine.
"""

import io
import json
import re
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.modules.email import dispatch
from app.modules.email.gmail import FakeMailbox
from app.worker import due
from tests.helpers import API, create_workspace, sign_in
from tests.test_email import (  # noqa: F401
    approve_policy,
    classify,
    connected,
    gmail,
    import_register,
    owner,
    run_sync,
)
from tests.test_import import import_and_commit, ok, workbook_bytes
from tests.test_prospects import row
from tests.test_research import candidates, run_to_end, start, world  # noqa: F401


def sql(tenant: UUID, statement: str, **params: Any) -> Any:
    with session_scope(RlsContext(tenant_id=tenant)) as db:
        result = db.execute(text(statement), {"t": tenant, **params})
        return [dict(r) for r in result.mappings()] if result.returns_rows else None


def figure(report: dict[str, Any], key: str) -> Any:
    return next(m for m in report["metrics"] if m["key"] == key)["value"]


@pytest.fixture
def other(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, "other@example.test")
    create_workspace(client, "Another customer")
    return client


def test_the_whole_journey_in_one_workspace_while_another_sees_none_of_it(
    owner: TestClient, other: TestClient, gmail: dict[str, Any], world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant: UUID = gmail["tenant"]
    box: FakeMailbox = gmail["box"]
    monkeypatch.setattr(get_settings(), "email_dispatch", "live")

    # 1. Import a list and look at the evidence.
    rows = [
        row("J-001", "Salon Aurora", 88, **{"Public business email": "office@example-salon.bg"}),
        row("J-002", "Vet Clinic Vita", 71, **{"Public business email": "hello@example-vet.bg"}),
        row("J-003", "Corner Bakery", 55),
    ]
    import_and_commit(owner, workbook_bytes(rows, shortlist=["J-001", "J-002"]))
    leads = {item["external_id"]: item for item in ok(owner.get(f"{API}/leads"))["items"]}
    assert len(leads) == 3
    aurora = ok(owner.get(f"{API}/prospects/{leads['J-001']['id']}"))
    assert aurora["verification_state"] == "unverified"  # imported is not verified
    if aurora["observation_id"]:
        verified = ok(owner.post(f"{API}/observations/{aurora['observation_id']}/verify", json={"state": "verified"}))
        assert verified["verification_state"] == "verified"

    # 2. Today's call queue, then a call whose outcome a person reports.
    queue = ok(owner.get(f"{API}/call-queue"))
    assert queue["entries"][0]["prospect"]["external_id"] == "J-001"  # highest score first
    phone = next(c for c in aurora["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{aurora['lead_id']}/calls", json={"channel_id": phone["id"]}), 201)
    assert call["outcome"] is None  # opening the dialler says nothing about what happened
    ok(
        owner.post(
            f"{API}/calls/{call['id']}/outcome",
            json={
                "outcome": "follow_up_requested",
                "follow_up_at": (
                    datetime.now(UTC) + timedelta(days=2)
                ).isoformat(),  # a callback booked for another day
                "follow_up_scope": "Online booking demo",
                "requested_email": "office@example-salon.bg",
            },
        )
    )
    # Booked for another day, so it leaves today's queue; the others stay.
    assert [e["prospect"]["external_id"] for e in ok(owner.get(f"{API}/call-queue"))["entries"]] == ["J-002", "J-003"]
    ok(
        owner.post(f"{API}/shortlists", json={"kind": "call_queue", "day": "today"}), 201
    )  # today's list, kept as a record
    # The callback comes due and shows up as needing attention.
    sql(tenant, "UPDATE tasks SET due_at = now() - interval '5 minutes' WHERE status = 'open'")
    attention = {i["key"]: i for i in ok(owner.get(f"{API}/workspace/attention"))["items"]}
    assert attention["follow_ups"]["count"] == 1

    # 3. Research with stand-in providers: candidates wait for a person; one is promoted.
    run = run_to_end(owner, world, start(owner, world))
    assert run["status"] == "completed" and "not padded" in (run["summary"]["shortfall_note"] or "not padded")
    found = candidates(owner, run_id=run["id"])
    assert found and ok(owner.get(f"{API}/leads"))["total"] == 3  # nothing became a lead by itself
    promotable = next(c for c in found if c["state"] in ("qualified", "needs_review") and c["name"])
    promoted = ok(owner.post(f"{API}/research-candidates/{promotable['id']}/promote", json={}))
    assert promoted["state"] == "promoted" and ok(owner.get(f"{API}/leads"))["total"] == 4

    # 4. Email: connect, approve the rules, draft, approve, send once.
    connected(owner, gmail)
    approve_policy(owner)
    import_register(owner, [])
    classify(owner, "office@example-salon.bg")
    sql(tenant, "UPDATE mailboxes SET min_send_interval_seconds = 0")
    first = ok(owner.post(f"{API}/email-drafts", json={"lead_id": aurora["lead_id"]}), 201)
    assert first["eligibility"]["outcome"] == "allow"
    ok(owner.post(f"{API}/email-drafts/{first['id']}/approve", json={}))
    intent = ok(owner.post(f"{API}/email-drafts/{first['id']}/send", json={}))
    assert [due.run_claimed(j) for j in due.claim_due(50) if j["kind"] == "email.send"] == ["done"]
    assert (
        len(box.sent) == 1
        and ok(owner.get(f"{API}/email-drafts/{first['id']}"))["send"]["state"] == "provider_accepted"
    )

    # 5. A second, unsolicited follow-up is scheduled; the prospect replies first; the follow-up is stopped.
    second = ok(
        owner.post(
            f"{API}/email-drafts",
            json={"lead_id": aurora["lead_id"], "subject": "Following up", "body_text": "Any thoughts?"},
        ),
        201,
    )
    ok(owner.post(f"{API}/email-drafts/{second['id']}/approve", json={}))
    later = (datetime.now(UTC) + timedelta(days=2)).astimezone().strftime("%Y-%m-%dT%H:%M")
    waiting = ok(owner.post(f"{API}/email-drafts/{second['id']}/send", json={"scheduled_local": later}))
    assert waiting["state"] == "queued"
    run_sync(gmail)
    sent_row = sql(tenant, "SELECT provider_thread_id, rfc_message_id FROM send_intents WHERE id = :i", i=intent["id"])[
        0
    ]
    box.add(
        sender="office@example-salon.bg",
        subject="Re: Salon Aurora",
        thread_id=sent_row["provider_thread_id"],
        text="Yes, send us the details.",
        headers={"In-Reply-To": sent_row["rfc_message_id"]},
    )
    run_sync(gmail)
    stopped = sql(tenant, "SELECT state, state_reason FROM send_intents WHERE id = :i", i=waiting["id"])[0]
    assert stopped["state"] == "cancelled" and "replied" in stopped["state_reason"]
    assert dispatch.process(tenant, UUID(waiting["id"])) is None and len(box.sent) == 1  # and it stays stopped
    assert ok(owner.get(f"{API}/leads/{aurora['lead_id']}"))["outreach_status"] == "replied"
    thread = ok(owner.get(f"{API}/email-threads", params={"lead_id": aurora["lead_id"]}))["items"][0]
    assert [m["direction"] for m in thread["messages"]] == ["outbound", "inbound"]
    ok(owner.post(f"{API}/email-threads/{thread['id']}/reply-outcome", json={"outcome": "positive"}))

    # 6. An opportunity, moved to won.
    deal = ok(
        owner.post(
            f"{API}/leads/{aurora['lead_id']}/convert",
            json={"title": "Online booking", "amount": "900", "currency": "EUR"},
        ),
        201,
    )
    won = next(s for s in ok(owner.get(f"{API}/pipelines"))[0]["stages"] if s["kind"] == "won")
    deal_id = deal.get("deal_id") or deal.get("converted_deal_id") or deal["id"]
    ok(owner.patch(f"{API}/deals/{deal_id}", json={"stage_id": won["id"]}))

    # 7. The report reflects what happened, from what the screens wrote.
    report = ok(owner.get(f"{API}/reports/funnel"))
    assert figure(report, "dialler_launches") == 1 and figure(report, "reported_calls") == 1
    assert figure(report, "follow_up_permissions") == 1 and figure(report, "callbacks_due") == 1
    assert figure(report, "emails_accepted") == 1 and figure(report, "replies") == 1
    assert figure(report, "positive_replies") == 100.0 and figure(report, "wins") == 1
    assert report["won_amounts"] == [{"currency": "EUR", "amount": "900.00", "basis": "entered on the opportunity"}]

    # 8. Export. The owner gets everything of this workspace and nothing of another's.
    archive = zipfile.ZipFile(io.BytesIO(owner.get(f"{API}/exports/workspace.zip").content))
    exported = json.loads(archive.read("companies.json"))
    assert "Salon Aurora" in {c["name"] for c in exported} and len(exported) == 4
    everything = b"".join(archive.read(name) for name in archive.namelist()).decode()
    assert "refresh-token" not in everything and "local-test-key" not in everything

    # Throughout, the other workspace has seen none of it.
    for path in (
        "/companies",
        "/leads",
        "/prospects",
        "/deals",
        "/tasks",
        "/research-runs",
        "/research-candidates",
        "/email-suppressions",
    ):
        body = ok(other.get(f"{API}{path}"))
        assert (body["total"] if isinstance(body, dict) else len(body)) == 0, path
    for path in (
        "/mailboxes",
        "/email-drafts",
        "/send-intents",
        "/research-configs",
        "/provider-connections",
        "/api-keys",
    ):
        assert ok(other.get(f"{API}{path}")) == [], path
    assert (
        ok(other.get(f"{API}/call-queue"))["entries"] == []
        and ok(other.get(f"{API}/workspace/attention"))["items"] == []
    )
    assert figure(ok(other.get(f"{API}/reports/funnel")), "emails_accepted") == 0
    other_archive = zipfile.ZipFile(io.BytesIO(other.get(f"{API}/exports/workspace.zip").content))
    assert json.loads(other_archive.read("companies.json")) == []
    assert "Aurora" not in b"".join(other_archive.read(n) for n in other_archive.namelist()).decode()


def test_no_record_of_one_workspace_can_be_reached_from_another_by_its_identifier(
    owner: TestClient, other: TestClient, gmail: dict[str, Any], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every route that takes an identifier is called by another workspace with this workspace's real identifiers."""
    tenant: UUID = gmail["tenant"]
    monkeypatch.setattr(get_settings(), "email_dispatch", "dry_run")
    import_and_commit(
        owner,
        workbook_bytes([row("I-001", "Salon Aurora", 88, **{"Public business email": "office@example-salon.bg"})]),
    )
    lead = ok(owner.get(f"{API}/leads"))["items"][0]
    detail = ok(owner.get(f"{API}/prospects/{lead['id']}"))
    connected(owner, gmail)
    box: FakeMailbox = gmail["box"]
    box.add(sender="office@example-salon.bg", subject="Hello")
    run_sync(gmail)
    thread = ok(owner.get(f"{API}/email-threads", params={"lead_id": lead["id"]}))["items"][0]
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    task = ok(owner.post(f"{API}/tasks", json={"title": "Call back", "lead_id": lead["id"]}), 201)
    note = ok(owner.post(f"{API}/notes", json={"body": "private note", "lead_id": lead["id"]}), 201)
    deal = ok(owner.post(f"{API}/deals", json={"company_id": lead["company_id"], "title": "Deal"}), 201)
    phone = next(c for c in detail["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{lead['id']}/calls", json={"channel_id": phone["id"]}), 201)
    key = ok(owner.post(f"{API}/api-keys", json={"name": "k", "scopes": ["crm.read"]}), 201)
    hook = ok(owner.post(f"{API}/webhook-endpoints", json={"url": "https://hooks.customer.example/crm"}), 201)
    delivery = ok(owner.post(f"{API}/webhook-endpoints/{hook['id']}/test"))
    suppression = ok(owner.post(f"{API}/email-suppressions", json={"value": "blocked@example.bg"}), 201)
    grant = ok(
        owner.post(
            f"{API}/support-grants", json={"grantee_email": "helper@example.test", "reason": "Looking into a problem"}
        ),
        201,
    )
    restriction = ok(
        owner.post(
            f"{API}/companies/{lead['company_id']}/restrictions", json={"channel_kind": "email", "reason": "Asked"}
        ),
        201,
    )
    upload = ok(
        owner.post(
            f"{API}/attachments",
            data={"company_id": lead["company_id"]},
            files={"file": ("a.txt", b"private", "text/plain")},
        ),
        201,
    )
    connection = ok(
        owner.post(
            f"{API}/provider-connections",
            json={"provider": "fake_model", "label": "m", "credential": "local-test-key-9"},
        ),
        201,
    )
    config = ok(
        owner.post(
            f"{API}/research-configs",
            json={
                "name": "c",
                "country": "Bulgaria",
                "cities": ["Sofia"],
                "categories": ["salon"],
                "cost_cap": "1",
                "candidate_cap": 5,
                "qualified_target": 2,
            },
        ),
        201,
    )
    pipeline = ok(owner.get(f"{API}/pipelines"))[0]
    imports = ok(owner.get(f"{API}/imports"))
    import_id = (imports["items"] if isinstance(imports, dict) else imports)[0]["id"]
    mailbox = ok(owner.get(f"{API}/mailboxes"))[0]
    member = ok(owner.get(f"{API}/members"))["items"][0]
    ids = {
        "company_id": lead["company_id"], "lead_id": lead["id"], "deal_id": deal["id"], "task_id": task["id"], "draft_id": draft["id"],
        "thread_id": thread["id"], "call_id": call["id"], "key_id": key["id"], "endpoint_id": hook["id"], "delivery_id": delivery["id"],
        "suppression_id": suppression["id"], "grant_id": grant["id"], "restriction_id": restriction["id"], "attachment_id": upload["id"],
        "connection_id": connection["id"], "config_id": config["id"], "pipeline_id": pipeline["id"], "stage_id": pipeline["stages"][0]["id"],
        "import_id": import_id, "mailbox_id": mailbox["id"], "user_id": member["user_id"], "channel_id": detail["channels"][0]["id"],
        "observation_id": detail["observation_id"], "note_id": note["id"],
    }  # fmt: skip
    before = {
        table: sql(tenant, f"SELECT count(*) AS n FROM {table}")[0]["n"]
        for table in (
            "companies",
            "leads",
            "deals",
            "tasks",
            "email_drafts",
            "api_keys",
            "webhook_endpoints",
            "attachments",
            "notes",
            "memberships",
        )
    }

    spec = client.get(f"{API}/openapi.json").json()
    tried, leaks = 0, []
    for full_path, operations in spec["paths"].items():
        names = re.findall(r"\{([^}]+)\}", full_path)
        if not names or any(
            n not in ids or ids[n] is None for n in names if n not in ("action", "entity_type", "entity_id", "row_id")
        ):
            continue
        concrete = full_path
        for name in names:
            value = {
                "action": "cancel",
                "entity_type": "companies",
                "entity_id": ids["company_id"],
                "row_id": str(uuid4()),
            }.get(name) or ids[name]
            concrete = concrete.replace("{" + name + "}", str(value))
        for method in operations:
            response = other.request(method.upper(), concrete, json={} if method in ("post", "put", "patch") else None)
            tried += 1
            if response.status_code < 400:
                leaks.append((method.upper(), full_path, response.status_code))
    assert tried > 60, tried  # the sweep really did cover the identifier-bearing routes
    assert leaks == [], leaks
    # Nothing in the first workspace changed as a result.
    after = {table: sql(tenant, f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in before}
    assert after == before
    assert ok(owner.get(f"{API}/companies/{lead['company_id']}"))["name"] == "Salon Aurora"
    assert (
        ok(owner.get(f"{API}/api-keys"))[0]["revoked_at"] is None
        and ok(owner.get(f"{API}/mailboxes"))[0]["status"] != "revoked"
    )
    # Search and filters cannot be used to look across either.
    for params in (
        {"q": "Aurora"},
        {"q": "%"},
        {"company_id": lead["company_id"]},
        {"owner_user_id": member["user_id"]},
    ):
        assert ok(other.get(f"{API}/leads", params=params))["total"] == 0, params
        assert ok(other.get(f"{API}/prospects", params={k: v for k, v in params.items() if k == "q"}))["total"] == 0
    bulk = other.post(
        f"{API}/companies/bulk", json={"ids": [lead["company_id"]], "action": "archive", "dry_run": False}
    )
    assert bulk.status_code >= 400 or bulk.json().get("affected", bulk.json().get("matched", 0)) == 0
    assert ok(owner.get(f"{API}/companies"))["total"] == 1


def test_many_simultaneous_requests_from_two_workspaces_never_cross(
    owner: TestClient, other: TestClient, make_client: Any
) -> None:
    """Connections are pooled and reused; a connection that just served one workspace must carry nothing over."""
    ok(owner.post(f"{API}/companies", json={"name": "Belongs to the first"}), 201)
    ok(other.post(f"{API}/companies", json={"name": "Belongs to the second"}), 201)
    sessions = {
        "first": (dict(owner.cookies), "Belongs to the first"),
        "second": (dict(other.cookies), "Belongs to the second"),
    }
    wrong: list[str] = []
    barrier = threading.Barrier(24)

    def worker(which: str) -> None:
        cookies, expected = sessions[which]
        client = make_client()
        client.cookies.update(cookies)
        barrier.wait()
        for _ in range(15):
            names = [c["name"] for c in ok(client.get(f"{API}/companies"))["items"]]
            if names != [expected]:
                wrong.append(f"{which} saw {names}")

    threads = [threading.Thread(target=worker, args=("first" if n % 2 else "second",)) for n in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wrong == []  # 360 requests over a shared pool


def test_lists_and_reports_stay_quick_with_a_realistic_volume(
    owner: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant = UUID(ok(owner.get(f"{API}/tenant"))["id"])
    sql(
        tenant,
        "INSERT INTO companies (tenant_id, name, city, country, source) SELECT :t, 'Company ' || g, CASE WHEN g % 3 = 0 THEN 'Sofia' ELSE 'Varna' END, "
        "'Bulgaria', 'import' FROM generate_series(1, 5000) g",
    )
    sql(
        tenant,
        "INSERT INTO leads (tenant_id, company_id, external_id, status, source) SELECT :t, c.id, 'P-' || row_number() OVER (), 'qualified', 'import' "
        "FROM companies c WHERE c.tenant_id = :t",
    )
    sql(
        tenant,
        "INSERT INTO call_attempts (tenant_id, company_id, lead_id, dialed_value, launched_at, outcome, outcome_reported_at) "
        "SELECT :t, l.company_id, l.id, '0888 000 000', now() - interval '1 day', 'no_answer', now() - interval '1 day' FROM leads l "
        "WHERE l.tenant_id = :t LIMIT 3000",
    )
    timings: dict[str, float] = {}
    for label, path, params in (
        ("companies, first page", "/companies", {"limit": 50}),
        ("companies, search", "/companies", {"q": "Company 49", "limit": 50}),
        ("companies, deep page", "/companies", {"limit": 50, "offset": 4900}),
        ("prospects, first page", "/prospects", {"limit": 50}),
        ("prospects, search", "/prospects", {"q": "Company 12", "limit": 50}),
        ("leads, first page", "/leads", {"limit": 50}),
        ("call queue", "/call-queue", {}),
        ("funnel report", "/reports/funnel", {}),
        ("daily attention", "/workspace/attention", {}),
    ):
        best = None
        for _ in range(3):
            started = time.perf_counter()
            response = owner.get(f"{API}{path}", params=params)
            elapsed = time.perf_counter() - started
            assert response.status_code == 200, (label, response.text[:200])
            best = elapsed if best is None else min(best, elapsed)
        timings[label] = round((best or 0) * 1000)
    with capsys.disabled():
        print("\nTIMINGS (ms, best of 3, 5,000 companies and leads, 3,000 calls): " + json.dumps(timings))
    slow = {label: ms for label, ms in timings.items() if ms > 1500}
    assert slow == {}, slow
