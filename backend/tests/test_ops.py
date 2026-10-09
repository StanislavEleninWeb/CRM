"""Reporting, retention, erasure, workspace deletion and support access."""

import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.db import RlsContext, session_scope
from app.core.storage import get_storage
from app.modules.dataops import service
from app.modules.email import sync
from app.modules.email.gmail import FakeMailbox
from app.worker import due
from tests.helpers import API, create_workspace, join, sign_in
from tests.test_import import import_and_commit, lead_row, ok, workbook_bytes

# October 2026 in Sofia: UTC+3 until the 25th, UTC+2 after.
OCT = {"from": "2026-10-01", "to": "2026-10-31"}


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
        result = db.execute(text(statement), {"t": tenant, **params})
        return [dict(r) for r in result.mappings()] if result.returns_rows else None


def metric(report: dict[str, Any], key: str) -> dict[str, Any]:
    return next(m for m in report["metrics"] if m["key"] == key)


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# --- reporting -------------------------------------------------------------------------------


def test_an_empty_workspace_reports_no_data_rather_than_zero_percent(owner: TestClient) -> None:
    report = ok(owner.get(f"{API}/reports/funnel", params=OCT))
    assert report["timezone"] == "Europe/Sofia" and "whole days in Europe/Sofia" in report["cohort_rule"]
    for key in ("connected_call_rate", "follow_up_completion", "response_time", "research_cost_per_qualified_lead"):
        m = metric(report, key)
        assert m["value"] is None, key  # nothing to divide by: not 0, not 100
        assert m["denominator"] == 0
    assert metric(report, "reported_calls")["value"] == 0  # a count of nothing is zero
    assert all(m["definition"] and m["basis"] for m in report["metrics"])
    assert report["won_amounts"] == [] and report["research_costs"] == []
    assert any("opens and clicks" in line for line in report["not_collected"])
    assert metric(report, "positive_replies")["value"] is None and metric(report, "meetings_booked")["value"] == 0
    assert owner.get(f"{API}/reports/funnel", params={"from": "2026-11-01", "to": "2026-10-01"}).status_code == 422
    assert owner.get(f"{API}/reports/funnel", params={"from": "2024-01-01", "to": "2026-10-01"}).status_code == 422


def test_report_totals_reconcile_with_a_known_fixture(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    company = UUID(ok(owner.post(f"{API}/companies", json={"name": "Salon Aurora"}), 201)["id"])
    stages = {
        r["kind"]: r for r in sql(tenant, "SELECT id, pipeline_id, kind, name FROM pipeline_stages ORDER BY position")
    }
    first_open = sql(
        tenant, "SELECT id, pipeline_id, name FROM pipeline_stages WHERE kind = 'open' ORDER BY position LIMIT 1"
    )[0]

    # Leads: three qualified in October (one from research), one in September, one October but not qualified.
    for status, source, created in (("qualified", "import", at(5, 9)), ("qualified", "research", at(12, 9)), ("qualified", "manual", at(31, 21, 30)),
                                    ("qualified", "import", at(30, 9, month=9)), ("needs_review", "research", at(15, 9))):  # fmt: skip
        sql(tenant, "INSERT INTO leads (tenant_id, company_id, status, source, created_at) VALUES (:t, :c, :s, :src, :at)",
            c=company, s=status, src=source, at=created)  # fmt: skip
    lead = sql(tenant, "SELECT id FROM leads ORDER BY created_at LIMIT 1")[0]["id"]

    # Calls. Seven dialler openings; five have a reported outcome in October; three of those reached someone.
    calls = [
        (at(6, 8), "connected", at(6, 8, 5)),
        (at(6, 9), "no_answer", at(6, 9, 1)),
        (at(7, 8), "follow_up_requested", at(7, 8, 9)),
        (at(8, 8), "not_interested", at(8, 8, 2)),
        (at(9, 8), "voicemail", at(9, 8, 1)),
        (at(10, 8), None, None),  # the dialler was opened and nothing was reported
        (at(11, 8), None, None),
        (at(30, 8, month=9), "connected", at(30, 8, 5, month=9)),  # September
        # 31 October 22:30 UTC is already 1 November in Sofia.
        (at(31, 22, 30), "connected", at(31, 22, 35)),
    ]
    for launched, outcome, reported in calls:
        sql(tenant, "INSERT INTO call_attempts (tenant_id, company_id, lead_id, dialed_value, launched_at, outcome, outcome_reported_at) "
                    "VALUES (:t, :c, :l, '0888 000 001', :la, :o, :r)", c=company, l=lead, la=launched, o=outcome, r=reported)  # fmt: skip

    # Follow-ups due in October: four, of which one cancelled, two done.
    for status, due_at, done in (("done", at(8, 9), at(8, 10)), ("done", at(9, 9), at(12, 10)), ("open", at(20, 9), None),
                                 ("cancelled", at(21, 9), None), ("done", at(2, 9, month=11), at(2, 10, month=11))):  # fmt: skip
        sql(tenant, "INSERT INTO tasks (tenant_id, title, kind, status, due_at, completed_at) VALUES (:t, 'Call back', 'follow_up', :s, :d, :c)",
            s=status, d=due_at, c=done)  # fmt: skip

    # Opportunities won in October in two currencies, one won in September, one still open.
    for title, stage, amount, currency, closed in (("A", "won", 1000, "EUR", at(10, 9)), ("B", "won", 500, "EUR", at(20, 9)),
                                                   ("C", "won", 300, "USD", at(21, 9)), ("D", "won", 9999, "EUR", at(1, 9, month=9)),
                                                   ("E", "open", 700, "EUR", None)):  # fmt: skip
        target = stages["won"] if stage == "won" else first_open
        sql(tenant, "INSERT INTO deals (tenant_id, company_id, pipeline_id, stage_id, title, amount, currency, closed_at, created_at) "
                    "VALUES (:t, :c, :p, :st, :title, :a, :cur, :closed, :created)", c=company, p=target["pipeline_id"], st=target["id"],
            title=title, a=amount, cur=currency, closed=closed, created=at(1, 9))  # fmt: skip

    # Research costs in October: estimated and verified in EUR, plus a reservation whose outcome is unknown.
    for amount, basis, when in (
        (2.5, "estimated", at(12, 9)),
        (1.5, "verified_invoice", at(13, 9)),
        (40, "estimated", at(1, 9, month=9)),
    ):
        sql(tenant, "INSERT INTO usage_ledger (tenant_id, provider, purpose, amount, currency, cost_basis, billed_to, occurred_at) "
                    "VALUES (:t, 'fake_discovery', 'research.discovery', :a, 'EUR', :b, 'tenant_provider_account', :at)", a=amount, b=basis, at=when)  # fmt: skip

    report = ok(owner.get(f"{API}/reports/funnel", params=OCT))
    values = {m["key"]: (m["value"], m.get("numerator"), m.get("denominator")) for m in report["metrics"]}
    assert values["qualified_leads"][0] == 3
    assert values["dialler_launches"][0] == 7 and values["reported_calls"][0] == 5  # opening the dialler is not a call
    assert values["connected_call_rate"] == (60.0, 3, 5)  # of reported calls, never of dialler openings
    assert values["follow_up_permissions"][0] == 1
    assert values["callbacks_due"][0] == 3 and values["follow_up_completion"] == (66.7, 2, 3)
    assert values["wins"][0] == 3
    assert report["won_amounts"] == [
        {"currency": "EUR", "amount": "1500.00", "basis": "entered on the opportunity"},
        {"currency": "USD", "amount": "300.00", "basis": "entered on the opportunity"},
    ]  # two currencies, two lines, no total
    assert values["research_cost_per_qualified_lead"] == (4.0, None, 1)  # (2.5 + 1.5) / one qualified research lead
    assert {(c["basis"], c["amount"]) for c in report["research_costs"]} == {
        ("estimated", "2.5000"),
        ("verified_invoice", "1.5000"),
    }
    assert metric(report, "reported_calls")["basis"] == "reported_by_people"
    assert metric(report, "dialler_launches")["basis"] == "recorded_by_system"
    won_stage = next(s for s in report["stages"] if s["kind"] == "won")
    open_stage = next(s for s in report["stages"] if s["stage"] == first_open["name"])
    assert won_stage["open_now"] == 0 and open_stage["open_now"] == 1 and open_stage["oldest_days_in_stage"] is not None

    # Every figure can be opened: the list has exactly as many records as the figure says.
    for key in ("qualified_leads", "dialler_launches", "reported_calls", "connected_calls", "follow_up_permissions", "callbacks_due",
                "callbacks_completed", "wins"):  # fmt: skip
        listed = ok(owner.get(f"{API}/reports/records", params={"metric": key, **OCT}))
        expected = values["connected_call_rate"][1] if key == "connected_calls" else (
            values["follow_up_completion"][1] if key == "callbacks_completed" else values[key][0])  # fmt: skip
        assert len(listed) == expected, key
    assert owner.get(f"{API}/reports/records", params={"metric": "made_up", **OCT}).status_code == 422

    # The time-zone edge: the 22:30 UTC call on the 31st belongs to 1 November in Sofia.
    november = ok(owner.get(f"{API}/reports/funnel", params={"from": "2026-11-01", "to": "2026-11-01"}))
    assert (
        metric(november, "reported_calls")["value"] == 1 and metric(november, "connected_call_rate")["value"] == 100.0
    )
    last_day = ok(owner.get(f"{API}/reports/funnel", params={"from": "2026-10-31", "to": "2026-10-31"}))
    assert metric(last_day, "qualified_leads")["value"] == 1  # 21:30 UTC is 23:30 in Sofia, still the 31st
    # A reservation whose charge is unknown is shown apart and noted, not folded in.
    ok(owner.put(f"{API}/budgets", json={"scope": "research", "limit_amount": "100"}))
    budget = sql(tenant, "SELECT id FROM budgets")[0]["id"]
    sql(tenant, "INSERT INTO budget_reservations (tenant_id, budget_id, idempotency_key, purpose, state, reserved_amount, currency, created_at) "
                "VALUES (:t, :b, 'report-fixture-reservation-0001', 'research.discovery', 'unknown', 60, 'EUR', :at)", b=budget, at=at(14, 9))  # fmt: skip
    with_unknown = ok(owner.get(f"{API}/reports/funnel", params=OCT))
    assert metric(with_unknown, "research_cost_per_qualified_lead")["value"] == 4.0
    assert "listed separately" in metric(with_unknown, "research_cost_per_qualified_lead")["note"]
    assert any(
        c["basis"].startswith("outcome unknown") and c["amount"] == "60.0000" for c in with_unknown["research_costs"]
    )
    # Mixed currencies: no per-lead figure is made up.
    sql(tenant, "INSERT INTO usage_ledger (tenant_id, provider, purpose, amount, currency, cost_basis, billed_to, occurred_at) "
                "VALUES (:t, 'x', 'research.model', 3, 'USD', 'estimated', 'tenant_provider_account', :at)", at=at(14, 9))  # fmt: skip
    mixed = metric(ok(owner.get(f"{API}/reports/funnel", params=OCT)), "research_cost_per_qualified_lead")
    assert mixed["value"] is None and "more than one currency" in mixed["note"]

    # Another workspace sees none of it; a member without the reports permission cannot ask.
    other = make_client()
    sign_in(other, "other@example.test")
    create_workspace(other, "Another customer")
    assert metric(ok(other.get(f"{API}/reports/funnel", params=OCT)), "dialler_launches")["value"] == 0
    assert ok(other.get(f"{API}/reports/records", params={"metric": "wins", **OCT})) == []


def test_the_daily_list_names_what_needs_someone(owner: TestClient, tenant: UUID) -> None:
    assert ok(owner.get(f"{API}/workspace/attention"))["items"] == []
    company = UUID(ok(owner.post(f"{API}/companies", json={"name": "Salon Aurora"}), 201)["id"])
    sql(
        tenant,
        "INSERT INTO leads (tenant_id, company_id, status, source) VALUES (:t, :c, 'qualified', 'import')",
        c=company,
    )
    sql(
        tenant,
        "INSERT INTO tasks (tenant_id, title, kind, status, due_at) VALUES (:t, 'Call back about booking', 'follow_up', 'open', now() - interval '1 hour')",
    )
    sql(
        tenant,
        "INSERT INTO tasks (tenant_id, title, kind, status, due_at) VALUES (:t, 'Next week', 'todo', 'open', now() + interval '6 days')",
    )
    stage = sql(tenant, "SELECT id, pipeline_id FROM pipeline_stages WHERE kind = 'open' ORDER BY position LIMIT 1")[0]
    sql(tenant, "INSERT INTO deals (tenant_id, company_id, pipeline_id, stage_id, title) VALUES (:t, :c, :p, :s, 'Booking system')",
        c=company, p=stage["pipeline_id"], s=stage["id"])  # fmt: skip
    items = {i["key"]: i for i in ok(owner.get(f"{API}/workspace/attention"))["items"]}
    assert set(items) == {"follow_ups", "unassigned"}  # the deal is new, the other task is not due
    assert items["follow_ups"]["count"] == 1 and items["follow_ups"]["examples"] == ["Call back about booking"]
    assert items["unassigned"]["link"] == "/prospects" and items["unassigned"]["examples"] == ["Salon Aurora"]


# --- retention -------------------------------------------------------------------------------


def test_retention_settings_have_bounds_and_the_purge_follows_them(
    owner: TestClient, tenant: UUID, make_client: Any
) -> None:
    settings = ok(owner.get(f"{API}/retention"))
    assert (
        settings["email_content_days"] == 730
        and settings["audit_days"] == 730
        and settings["bounds"]["audit_days"] == [365, 3650]
    )
    assert any("backups" in line.lower() for line in settings["not_covered"])
    assert (
        owner.put(f"{API}/retention", json={"audit_days": 30}).status_code == 422
    )  # the security record is kept at least a year
    assert owner.put(f"{API}/retention", json={"email_content_days": 5}).status_code == 422
    assert owner.put(f"{API}/retention", json={"forever": True}).status_code == 422
    assert (
        ok(owner.put(f"{API}/retention", json={"email_content_days": 60, "webhook_delivery_days": 7}))[
            "email_content_days"
        ]
        == 60
    )
    assert "retention.changed" in [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]
    manager = make_client()
    sign_in(manager, "manager@example.test")
    join(owner, manager, "manager@example.test", "sales_manager")
    assert manager.get(f"{API}/retention").status_code == 403

    mailbox, thread = uuid4(), uuid4()
    sql(
        tenant,
        "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status) VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active')",
        m=mailbox,
    )
    sql(
        tenant,
        "INSERT INTO email_threads (id, tenant_id, mailbox_id, subject) VALUES (:th, :t, :m, 'Old conversation')",
        th=thread,
        m=mailbox,
    )
    for name, age in (("old", 90), ("recent", 10)):
        sql(tenant, "INSERT INTO email_messages (tenant_id, mailbox_id, thread_id, provider_message_id, direction, subject, snippet, body_text, body_html, "
                    "attachments, sent_at) VALUES (:t, :m, :th, :pid, 'inbound', 'Subject kept', 'snip', 'private text', '<p>private</p>', "
                    "'[{\"filename\": \"x.pdf\"}]'::jsonb, now() - make_interval(days => :age))", m=mailbox, th=thread, pid=name, age=age)  # fmt: skip
    key = ok(owner.post(f"{API}/api-keys", json={"name": "k", "scopes": ["crm.read"]}), 201)
    for name, hours in (("old", 30), ("new", 1)):
        sql(tenant, "INSERT INTO idempotency_keys (tenant_id, api_key_id, key, request_hash, created_at) VALUES (:t, :k, :key, 'h', now() - make_interval(hours => :h))",
            k=key["id"], key=name, h=hours)  # fmt: skip
    sql(
        tenant,
        "INSERT INTO due_jobs (tenant_id, kind, unique_key, due_at, status, finished_at) VALUES (:t, 'x.done', 'old', now(), 'done', now() - interval '20 days')",
    )
    sql(
        tenant,
        "INSERT INTO due_jobs (tenant_id, kind, unique_key, due_at, status, finished_at) VALUES (:t, 'x.done', 'new', now(), 'done', now() - interval '2 days')",
    )

    daily = sql(tenant, "SELECT status FROM due_jobs WHERE kind = 'retention.purge'")
    assert [j["status"] for j in daily] == ["pending"]  # scheduled for every workspace when it is created
    removed = service.purge(tenant)
    assert removed["email contents"] == 1 and removed["idempotency records"] == 1 and removed["finished jobs"] == 1
    messages = {m["provider_message_id"]: m for m in sql(tenant, "SELECT * FROM email_messages")}
    old, recent = messages["old"], messages["recent"]
    assert (old["body_text"], old["body_html"], old["snippet"], old["attachments"]) == (None, None, None, [])
    assert (
        old["subject"] == "Subject kept" and recent["body_text"] == "private text"
    )  # the line stays; only old content goes
    assert [r["key"] for r in sql(tenant, "SELECT key FROM idempotency_keys")] == ["new"]
    assert service.purge(tenant)["email contents"] == 0  # nothing left to do


# --- erasure ---------------------------------------------------------------------------------


def test_an_erased_business_is_gone_and_does_not_come_back(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    rows = [
        lead_row(
            "X-001",
            "Salon Aurora",
            **{"Public business email": "office@example-salon.bg", "Website URL": "https://example-salon.bg"},
        ),
        lead_row(
            "X-002",
            "Vet Clinic Vita",
            **{"Public business email": "hello@example-vet.bg", "Website URL": "https://example-vet.bg"},
        ),
    ]
    import_and_commit(owner, workbook_bytes(rows))
    leads = {item["external_id"]: item for item in ok(owner.get(f"{API}/leads"))["items"]}
    company_id = leads["X-001"]["company_id"]
    upload = owner.post(
        f"{API}/attachments",
        data={"company_id": company_id},
        files={"file": ("offer.txt", b"private offer", "text/plain")},
    )
    storage_key = sql(tenant, "SELECT storage_key FROM attachments")[0]["storage_key"]
    assert upload.status_code == 201 and get_storage().get(storage_key) == b"private offer"
    ok(owner.post(f"{API}/notes", json={"body": "Spoke to Maria about booking", "company_id": company_id}), 201)
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": leads["X-001"]["id"]}), 201)

    rep = make_client()
    sign_in(rep, "rep@example.test")
    join(owner, rep, "rep@example.test", "representative")
    body = {"reason": "Erasure requested by the business by email on 9 October", "confirm_name": "Salon Aurora"}
    assert rep.post(f"{API}/companies/{company_id}/erase", json=body).status_code == 403
    assert owner.post(f"{API}/companies/{company_id}/erase", json={**body, "confirm_name": "Salon"}).status_code == 422
    assert owner.post(f"{API}/companies/{company_id}/erase", json={**body, "reason": "asked"}).status_code == 422
    result = ok(owner.post(f"{API}/companies/{company_id}/erase", json=body))
    assert result["erased"] is True and result["removed"]["company"] == 1 and result["removed"]["files"] == 1

    assert owner.get(f"{API}/companies/{company_id}").status_code == 404
    assert [item["external_id"] for item in ok(owner.get(f"{API}/leads"))["items"]] == ["X-002"]
    assert owner.get(f"{API}/email-drafts/{draft['id']}").status_code == 404
    for table in ("notes", "attachments", "contact_channels", "activities"):
        left = sql(tenant, f"SELECT count(*) AS n FROM {table} WHERE company_id = :c", c=company_id)
        assert left[0]["n"] == 0, table
    for table in ("lead_assessments", "observations", "lead_scores"):
        left = sql(tenant, f"SELECT count(*) AS n FROM {table} WHERE lead_id = :l", l=leads["X-001"]["id"])
        assert left[0]["n"] == 0, table
    # The imported row and the uploaded list that held it are gone too; the other business's row is kept.
    kept_rows = sql(tenant, "SELECT external_id, raw::text AS raw FROM import_rows")
    assert [r["external_id"] for r in kept_rows] == ["X-002"] and "office@example-salon.bg" not in json.dumps(kept_rows)
    assert result["removed"]["uploaded lists removed from storage"] == 1
    with pytest.raises(Exception):  # noqa: B017 - the object is gone from storage
        get_storage().get(storage_key)
    # What is kept: that this address must not be emailed, and hashes that identify nothing by themselves.
    suppressed = ok(owner.get(f"{API}/email-suppressions"))["items"]
    assert [(s["value"], s["note"]) for s in suppressed] == [("office@example-salon.bg", "Erased on request")]
    stones = sql(tenant, "SELECT kind, value_hash FROM erasure_tombstones ORDER BY kind")
    assert {s["kind"] for s in stones} >= {"email", "domain", "external_id"}
    dump = json.dumps(stones) + json.dumps(ok(owner.get(f"{API}/audit-events")))
    assert "example-salon" not in dump and "Aurora" not in dump and "X-001" not in dump
    erased_event = next(e for e in ok(owner.get(f"{API}/audit-events"))["items"] if e["action"] == "company.erased")
    assert erased_event["data"]["reason"].startswith("Erasure requested")

    # 1. The same list is imported again: the erased business is skipped, the other is untouched.
    again = import_and_commit(owner, workbook_bytes(rows))
    assert [item["external_id"] for item in ok(owner.get(f"{API}/leads"))["items"]] == ["X-002"]
    assert ok(owner.get(f"{API}/companies"))["total"] == 1 and again["status"] == "committed"
    # ... also under a new list number, because the website and address are remembered.
    renumbered = [
        lead_row(
            "Z-900",
            "Aurora Salon (new list)",
            **{"Public business email": "office@example-salon.bg", "Website URL": "https://example-salon.bg"},
        )
    ]
    import_and_commit(owner, workbook_bytes(renumbered))
    assert ok(owner.get(f"{API}/companies"))["total"] == 1
    # ... and with only the email address in common: no website, a new number, a different name.
    by_email = [lead_row("Q-777", "Completely Different Name", **{"Public business email": "OFFICE@example-salon.bg"})]
    import_and_commit(owner, workbook_bytes(by_email))
    assert ok(owner.get(f"{API}/companies"))["total"] == 1
    # 2. Mail from that address arrives in the mailbox: it is not stored.
    mailbox = uuid4()
    sql(
        tenant,
        "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status) VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active')",
        m=mailbox,
    )
    box = FakeMailbox("sales@seweb.example")
    theirs = box.add(sender="office@example-salon.bg", subject="Following up")
    ours = box.add(sender="hello@example-vet.bg", subject="A question")
    with session_scope(RlsContext(tenant_id=tenant)) as db:
        row = db.execute(text("SELECT * FROM mailboxes WHERE id = :m"), {"m": mailbox}).mappings().one()
        assert sync.store_message(db, tenant, row, theirs) == 0 and sync.store_message(db, tenant, row, ours) == 1
    assert [m["from_address"] for m in sql(tenant, "SELECT from_address::text FROM email_messages")] == [
        "hello@example-vet.bg"
    ]
    # 3. Research cannot add it back.
    assert service.is_erased_for_test(tenant, [("domain", "example-salon.bg")]) is True
    assert service.is_erased_for_test(tenant, [("domain", "example-vet.bg")]) is False
    # 4. The hashes are per workspace: the same business in another workspace is not affected.
    other = make_client()
    sign_in(other, "other@example.test")
    other_tenant = UUID(create_workspace(other, "Another customer"))
    assert service.is_erased_for_test(other_tenant, [("domain", "example-salon.bg")]) is False
    import_and_commit(other, workbook_bytes(rows))
    assert ok(other.get(f"{API}/companies"))["total"] == 2


# --- workspace export and deletion -----------------------------------------------------------


def test_the_owner_can_export_everything_except_secrets(owner: TestClient, tenant: UUID, make_client: Any) -> None:
    ok(owner.post(f"{API}/companies", json={"name": "Salon Aurora"}), 201)
    ok(
        owner.post(
            f"{API}/provider-connections",
            json={"provider": "fake_model", "label": "m", "credential": "sk-live-SECRET-VALUE-000000000"},
        ),
        201,
    )
    key = ok(owner.post(f"{API}/api-keys", json={"name": "k", "scopes": ["crm.read"]}), 201)["key"]
    response = owner.get(f"{API}/exports/workspace.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["files"]["companies.json"] == 1 and manifest["tenant_id"] == str(tenant)
    assert json.loads(archive.read("companies.json"))[0]["name"] == "Salon Aurora"
    everything = b"".join(archive.read(name) for name in archive.namelist()).decode()
    assert "sk-live-SECRET" not in everything and key not in everything and "ciphertext" not in everything
    assert "provider_connections.json" not in archive.namelist() and "api_keys.json" not in archive.namelist()
    assert "workspace.exported" in [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]
    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    assert admin.get(f"{API}/exports/workspace.zip").status_code == 403


def test_workspace_deletion_waits_can_be_cancelled_and_then_removes_everything(
    owner: TestClient, tenant: UUID, make_client: Any, migrator_engine: Any
) -> None:
    other = make_client()
    sign_in(other, "other@example.test")
    other_tenant = UUID(create_workspace(other, "Another customer"))
    ok(other.post(f"{API}/companies", json={"name": "Not involved"}), 201)
    company = ok(owner.post(f"{API}/companies", json={"name": "Salon Aurora"}), 201)
    owner.post(
        f"{API}/attachments", data={"company_id": company["id"]}, files={"file": ("a.txt", b"file body", "text/plain")}
    )
    storage_key = sql(tenant, "SELECT storage_key FROM attachments")[0]["storage_key"]
    key = ok(owner.post(f"{API}/api-keys", json={"name": "k", "scopes": ["crm.read"]}), 201)["key"]
    agent = make_client()
    agent.headers["Authorization"] = f"Bearer {key}"

    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    assert admin.post(f"{API}/tenant/deletion", json={"confirm_name": "SEWEB"}).status_code == 403  # the owner only
    assert owner.post(f"{API}/tenant/deletion", json={"confirm_name": "seweb"}).status_code == 422
    requested = ok(owner.post(f"{API}/tenant/deletion", json={"confirm_name": "SEWEB"}))
    wait = datetime.fromisoformat(requested["due_at"]) - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < wait <= timedelta(days=7) and any(
        "backups" in line for line in requested["what_happens"]
    )

    def run_deletion() -> list[str]:
        sql(tenant, "UPDATE due_jobs SET due_at = now() WHERE kind = 'tenant.delete' AND status = 'pending'")
        return [due.run_claimed(job) for job in due.claim_due(50) if job["kind"] == "tenant.delete"]

    # Run early (a clock problem, say): it is not due, so nothing is deleted.
    assert run_deletion() == ["rescheduled"] and ok(owner.get(f"{API}/companies"))["total"] == 1
    # Cancelled: the job does nothing, even if it runs.
    assert ok(owner.delete(f"{API}/tenant/deletion"))["due_at"] is None
    sql(tenant, "UPDATE due_jobs SET status = 'pending', due_at = now() WHERE kind = 'tenant.delete'")
    assert [due.run_claimed(j) for j in due.claim_due(50) if j["kind"] == "tenant.delete"] == ["done"]
    assert ok(owner.get(f"{API}/companies"))["total"] == 1 and agent.get(f"{API}/companies").status_code == 200

    ok(owner.post(f"{API}/tenant/deletion", json={"confirm_name": "SEWEB"}))
    sql(tenant, "UPDATE tenants SET deletion_due_at = now() - interval '1 minute' WHERE id = :t")
    run_deletion()
    with migrator_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants WHERE id = :t"), {"t": tenant}).scalar() == 0
        conn.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant)})
        for table in ("companies", "attachments", "api_keys", "activities", "audit_events", "due_jobs"):
            assert (
                conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0
            ), table
        assert (
            conn.execute(
                text("SELECT count(*) FROM tenant_deletions WHERE deleted_tenant_id = :t"), {"t": tenant}
            ).scalar()
            == 1
        )
    with pytest.raises(Exception):  # noqa: B017
        get_storage().get(storage_key)
    assert agent.get(f"{API}/companies").status_code == 401  # the key died with the workspace
    assert owner.get(f"{API}/companies").status_code == 403
    assert ok(other.get(f"{API}/companies"))["total"] == 1 and other_tenant  # another workspace is untouched


# --- support access --------------------------------------------------------------------------


def test_support_access_is_granted_by_the_owner_limited_logged_and_expires(
    owner: TestClient, tenant: UUID, make_client: Any
) -> None:
    company = ok(owner.post(f"{API}/companies", json={"name": "Salon Aurora"}), 201)
    support = make_client()
    sign_in(support, "other@example.test")
    create_workspace(support, "Support staff home")
    switch = {"tenant_id": str(tenant)}
    # By default nobody outside the workspace can open it, whoever they are.
    assert support.post(f"{API}/auth/switch-tenant", json=switch).status_code == 403

    grant = {"grantee_email": "Other@Example.test", "hours": 2, "reason": "Investigating why the import shows no rows"}
    assert owner.post(f"{API}/support-grants", json={**grant, "hours": 100}).status_code == 422  # at most 72 hours
    assert owner.post(f"{API}/support-grants", json={**grant, "reason": "help"}).status_code == 422
    # An address with no account is accepted like any other, so the form cannot be used to find out who has one.
    unknown = ok(owner.post(f"{API}/support-grants", json={**grant, "grantee_email": "nobody@example.test"}), 201)
    ok(owner.delete(f"{API}/support-grants/{unknown['id']}"))
    assert owner.post(f"{API}/support-grants", json={**grant, "grantee_email": "not an address"}).status_code == 422
    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    assert admin.post(f"{API}/support-grants", json=grant).status_code == 403  # the owner decides
    assert owner.post(f"{API}/support-grants", json={**grant, "grantee_email": "admin@example.test"}).status_code == 409
    created = ok(owner.post(f"{API}/support-grants", json=grant), 201)
    assert created["active"] is True and created["include_communications"] is False and created["first_used_at"] is None
    assert ok(owner.get(f"{API}/entitlements"))["usage"]["seats"] == 2  # support access is not a seat

    assert support.post(f"{API}/auth/switch-tenant", json=switch).status_code == 204
    assert ok(support.get(f"{API}/companies"))["items"][0]["name"] == "Salon Aurora"
    assert ok(support.get(f"{API}/reports/funnel"))["timezone"] == "Europe/Sofia"
    # Read-only, and not the sensitive parts.
    for method, path, body in (
        ("POST", "/companies", {"name": "Changed by support"}),
        ("PATCH", f"/companies/{company['id']}", {"name": "Renamed"}),
        ("DELETE", f"/companies/{company['id']}", None),
        ("GET", "/email-threads", None),
        ("GET", "/email-drafts", None),
        ("GET", "/audit-events", None),
        ("GET", "/api-keys", None),
        ("GET", "/provider-connections", None),
        ("GET", "/billing", None),
        ("GET", "/support-grants", None),
        ("GET", "/exports/workspace.zip", None),
        ("GET", "/exports/prospects.xlsx", None),
        ("GET", "/members", None),
    ):
        assert support.request(method, f"{API}{path}", json=body).status_code == 403, path
    # The owner sees that access started, when, and by whom.
    events = ok(owner.get(f"{API}/audit-events"))["items"]
    started = [e for e in events if e["action"] == "support.access_started"]
    assert len(started) == 1 and started[0]["actor_type"] == "support" and started[0]["target_id"] == created["id"]
    assert "support.access_granted" in [e["action"] for e in events]
    used = next(g for g in ok(owner.get(f"{API}/support-grants")) if g["id"] == created["id"])
    assert used["first_used_at"] is not None

    # Communications only when the owner says so.
    with_mail = ok(owner.post(f"{API}/support-grants", json={**grant, "include_communications": True}), 201)
    assert support.get(f"{API}/email-threads", params={"needs_review": True}).status_code == 200
    ok(owner.delete(f"{API}/support-grants/{with_mail['id']}"))
    assert support.get(f"{API}/email-threads").status_code == 403 and support.get(f"{API}/companies").status_code == 200

    # It ends by itself.
    with session_scope(RlsContext(tenant_id=tenant)) as db:
        db.execute(
            text(
                "UPDATE support_grants SET created_at = now() - interval '3 hours', expires_at = now() - interval '1 minute' WHERE revoked_at IS NULL"
            )
        )
    assert support.get(f"{API}/companies").status_code == 403
    assert support.post(f"{API}/auth/switch-tenant", json=switch).status_code == 403
    assert not any(g["active"] for g in ok(owner.get(f"{API}/support-grants")))
    # And an ordinary member never sees the audit log.
    rep = make_client()
    sign_in(rep, "rep@example.test")
    join(owner, rep, "rep@example.test", "representative")
    assert rep.get(f"{API}/audit-events").status_code == 403 and rep.get(f"{API}/support-grants").status_code == 403


def test_erasure_waits_for_an_email_in_flight_and_cancels_one_that_is_waiting(owner: TestClient, tenant: UUID) -> None:
    row = lead_row("E-001", "Salon Aurora", **{"Public business email": "office@example-salon.bg"})
    import_and_commit(owner, workbook_bytes([row]))
    lead = ok(owner.get(f"{API}/leads"))["items"][0]
    mailbox, draft, intent = uuid4(), uuid4(), uuid4()
    sql(
        tenant,
        "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status) "
        "VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active')",
        m=mailbox,
    )
    sql(
        tenant,
        "INSERT INTO email_drafts (id, tenant_id, mailbox_id, lead_id, company_id, kind, to_address, subject, body_text, status) "
        "VALUES (:d, :t, :m, :l, :c, 'unsolicited', 'office@example-salon.bg', 's', 'b', 'queued')",
        d=draft,
        m=mailbox,
        l=lead["id"],
        c=lead["company_id"],
    )
    sql(
        tenant,
        "INSERT INTO send_intents (id, tenant_id, draft_id, mailbox_id, draft_version, content_hash, to_address, kind, "
        "scheduled_for, rfc_message_id, state) VALUES (:i, :t, :d, :m, 1, 'h', 'office@example-salon.bg', 'unsolicited', "
        "now(), '<x@seweb.example>', 'dispatching')",
        i=intent,
        d=draft,
        m=mailbox,
    )
    body = {"reason": "Erasure requested by the business", "confirm_name": "Salon Aurora"}
    for state in ("dispatching", "unknown"):
        sql(tenant, "UPDATE send_intents SET state = :s", s=state)
        refused = owner.post(f"{API}/companies/{lead['company_id']}/erase", json=body)
        assert refused.status_code == 409 and "Settle it under Email" in refused.json()["error"]["message"], state
        assert sql(tenant, "SELECT count(*) AS n FROM send_intents")[0]["n"] == 1  # the record to settle is kept
    assert ok(owner.get(f"{API}/companies"))["total"] == 1
    # A message that has not started sending is cancelled, then removed with everything else.
    sql(tenant, "UPDATE send_intents SET state = 'queued'")
    ok(owner.post(f"{API}/companies/{lead['company_id']}/erase", json=body))
    assert sql(tenant, "SELECT count(*) AS n FROM send_intents")[0]["n"] == 0
    assert ok(owner.get(f"{API}/companies"))["total"] == 0
    cancelled = sql(tenant, "SELECT payload FROM outbox_events WHERE event_type = 'email.send.cancelled'")
    assert cancelled and "erased on request" in cancelled[0]["payload"]["reason"]


def test_support_access_can_read_only_what_is_on_its_list(
    owner: TestClient, tenant: UUID, make_client: Any, client: TestClient
) -> None:
    from app.core import access_policy

    support = make_client()
    sign_in(support, "other@example.test")
    create_workspace(support, "Support staff home")
    grant = {"grantee_email": "other@example.test", "hours": 1, "reason": "Checking a display problem"}
    ok(owner.post(f"{API}/support-grants", json=grant), 201)
    assert support.post(f"{API}/auth/switch-tenant", json={"tenant_id": str(tenant)}).status_code == 204
    spec = client.get(f"{API}/openapi.json").json()
    # Routes that do not act inside a workspace at all.
    outside = {
        "/auth/me", "/auth/sessions", "/auth/login", "/auth/callback", "/invitations/lookup", "/system/info",
        "/system/readiness", "/mailboxes/gmail/callback",
    }  # fmt: skip
    opened, refused = set(), set()
    for full_path, operations in spec["paths"].items():
        template = full_path.removeprefix(API)
        if "get" not in operations or template in outside:
            continue
        concrete = re.sub(r"\{[^}]+\}", str(uuid4()), full_path)
        response = support.get(concrete, params={"metric": "replies", "needs_review": True}, follow_redirects=False)
        (refused if response.status_code == 403 else opened).add(template)
    # Every read route was tried, and exactly the listed ones are open. A new route is closed until someone lists it.
    assert opened == set(access_policy.SUPPORT_READABLE), opened ^ set(access_policy.SUPPORT_READABLE)
    assert set(access_policy.SUPPORT_COMMUNICATIONS) <= refused
    for path in (
        "/send-intents", "/workspace/attention", "/reports/records", "/activities", "/email-suppressions", "/mailboxes",
        "/email-sending", "/notes", "/audit-events", "/members", "/api-keys", "/billing", "/retention", "/tenant/deletion",
    ):  # fmt: skip
        assert path in refused, path
    # With communications included, those open and nothing else does.
    ok(owner.post(f"{API}/support-grants", json={**grant, "include_communications": True}), 201)
    assert support.get(f"{API}/send-intents").status_code == 200
    assert support.get(f"{API}/activities").status_code != 403  # open; it then asks which record
    assert support.get(f"{API}/audit-events").status_code == 403
    assert support.get(f"{API}/api-keys").status_code == 403


def test_the_report_counts_what_the_application_itself_records(owner: TestClient, tenant: UUID) -> None:
    """Built through the API only, so a figure cannot silently stop matching what the screens write."""
    import_and_commit(owner, workbook_bytes([lead_row("R-001", "Salon Aurora")]))
    lead = ok(owner.get(f"{API}/leads"))["items"][0]
    me = ok(owner.get(f"{API}/auth/me"))["user"]["id"]
    phone = next(c for c in ok(owner.get(f"{API}/prospects/{lead['id']}"))["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{lead['id']}/calls", json={"channel_id": phone["id"]}), 201)
    follow_up = {
        "outcome": "follow_up_requested",
        "follow_up_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "follow_up_scope": "Booking demo",
    }
    ok(owner.post(f"{API}/calls/{call['id']}/outcome", json=follow_up))
    ok(owner.post(f"{API}/tasks", json={"title": "Demo with the owner", "kind": "meeting", "lead_id": lead["id"]}), 201)
    pipeline = ok(owner.get(f"{API}/pipelines"))[0]
    proposal = next((s for s in pipeline["stages"] if s["name"].lower().startswith("proposal")), None)
    won = next(s for s in pipeline["stages"] if s["kind"] == "won")
    new_deal = {"company_id": lead["company_id"], "title": "Booking system", "amount": "1200", "currency": "EUR"}
    deal = ok(owner.post(f"{API}/deals", json=new_deal), 201)
    if proposal is not None:
        ok(owner.patch(f"{API}/deals/{deal['id']}", json={"stage_id": proposal["id"]}))
    ok(owner.patch(f"{API}/deals/{deal['id']}", json={"stage_id": won["id"]}))

    report = ok(owner.get(f"{API}/reports/funnel"))
    assert metric(report, "dialler_launches")["value"] == 1 and metric(report, "reported_calls")["value"] == 1
    assert metric(report, "connected_call_rate")["value"] == 100.0
    assert metric(report, "follow_up_permissions")["value"] == 1
    assert metric(report, "callbacks_due")["value"] == 1  # the task the call outcome created, whatever kind it has
    completion = metric(report, "follow_up_completion")
    assert completion["value"] == 0.0 and completion["denominator"] == 1
    assert metric(report, "meetings_booked")["value"] == 1
    assert metric(report, "wins")["value"] == 1
    assert report["won_amounts"] == [{"currency": "EUR", "amount": "1200.00", "basis": "entered on the opportunity"}]
    if proposal is not None:
        assert metric(report, "proposals")["value"] == 1
    task = ok(owner.get(f"{API}/reports/records", params={"metric": "callbacks_due"}))[0]
    ok(owner.patch(f"{API}/tasks/{task['id']}", json={"status": "done"}))
    assert metric(ok(owner.get(f"{API}/reports/funnel")), "follow_up_completion")["value"] == 100.0
    # Only what one member did, on request. Someone who did nothing has no data rather than zero percent.
    mine = ok(owner.get(f"{API}/reports/funnel", params={"owner_user_id": me}))
    assert metric(mine, "reported_calls")["value"] == 1
    nobody = ok(owner.get(f"{API}/reports/funnel", params={"owner_user_id": str(uuid4())}))
    assert metric(nobody, "reported_calls")["value"] == 0 and metric(nobody, "connected_call_rate")["value"] is None

    # A reply is positive only when a person says so.
    mailbox, thread = uuid4(), uuid4()
    sql(
        tenant,
        "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status) "
        "VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active')",
        m=mailbox,
    )
    sql(
        tenant,
        "INSERT INTO email_threads (id, tenant_id, mailbox_id, subject, lead_id, company_id, link_state, has_inbound) "
        "VALUES (:th, :t, :m, 'Re: booking', :l, :c, 'linked', true)",
        th=thread,
        m=mailbox,
        l=lead["id"],
        c=lead["company_id"],
    )
    sql(
        tenant,
        "INSERT INTO email_messages (tenant_id, mailbox_id, thread_id, provider_message_id, direction, classification, subject, "
        "body_text, sent_at) VALUES (:t, :m, :th, 'r1', 'inbound', 'reply', 'Re: booking', 'Yes please, we would love a demo!', now())",
        m=mailbox,
        th=thread,
    )
    before = ok(owner.get(f"{API}/reports/funnel"))
    assert metric(before, "replies")["value"] == 1
    assert metric(before, "positive_replies")["value"] is None  # enthusiastic wording changes nothing
    marked = ok(owner.post(f"{API}/email-threads/{thread}/reply-outcome", json={"outcome": "positive"}))
    assert marked["reply_outcome"] == "positive"
    after = metric(ok(owner.get(f"{API}/reports/funnel")), "positive_replies")
    assert (after["value"], after["numerator"], after["denominator"]) == (100.0, 1, 1)


def test_the_security_log_is_purged_only_past_its_retention(
    owner: TestClient, tenant: UUID, migrator_engine: Any
) -> None:
    ok(owner.put(f"{API}/retention", json={"audit_days": 365}))
    with migrator_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        for action, age in (("old.event", 400), ("recent.event", 300)):
            conn.execute(
                text(
                    "INSERT INTO audit_events (tenant_id, actor_type, action, target_type, origin, occurred_at) "
                    "VALUES (:t, 'system', :a, 'test', 'api', now() - make_interval(days => :d))"
                ),
                {"t": tenant, "a": action, "d": age},
            )
    service.purge(tenant)
    actions = [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]
    assert "recent.event" in actions and "old.event" not in actions
    # The application's own database role still cannot delete from the log directly, or purge below a year.
    with session_scope(RlsContext(tenant_id=tenant)) as db, pytest.raises(Exception, match="permission denied"):
        db.execute(text("DELETE FROM audit_events"))
    with session_scope(RlsContext(tenant_id=tenant)) as db, pytest.raises(Exception, match="365 days"):
        db.execute(text("SELECT audit_events_purge(30)"))
    with session_scope(RlsContext(tenant_id=tenant)) as db, pytest.raises(Exception, match="permission denied"):
        db.execute(text("SELECT * FROM billing_events"))


def test_monitoring_figures_are_counts_across_workspaces_behind_a_token(
    owner: TestClient, tenant: UUID, make_client: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    # Closed unless a token is configured, and then only to its holder. A wrong token looks like a missing page.
    assert client.get("/ops/metrics").status_code == 404
    monkeypatch.setattr(get_settings(), "ops_metrics_token", "monitoring-token-for-tests-0001")
    assert client.get("/ops/metrics").status_code == 404
    assert client.get("/ops/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    assert owner.get("/ops/metrics").status_code == 404  # being signed in is not enough

    other = make_client()
    sign_in(other, "other@example.test")
    other_tenant = UUID(create_workspace(other, "Another customer"))
    for workspace in (tenant, other_tenant):
        mailbox, draft = uuid4(), uuid4()
        sql(
            workspace,
            "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status, last_synced_at, watch_expires_at) "
            "VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active', now() - interval '2 hours', now() + interval '3 hours')",
            m=mailbox,
        )
        sql(
            workspace,
            "INSERT INTO email_drafts (id, tenant_id, mailbox_id, kind, to_address, subject, body_text, status) "
            "VALUES (:d, :t, :m, 'reply', 'private.person@example.bg', 'Private subject', 'b', 'queued')",
            d=draft,
            m=mailbox,
        )
        sql(
            workspace,
            "INSERT INTO send_intents (tenant_id, draft_id, mailbox_id, draft_version, content_hash, to_address, kind, scheduled_for, "
            "rfc_message_id, state, dispatched_at) VALUES (:t, :d, :m, 1, 'h', 'private.person@example.bg', 'reply', now(), :rfc, "
            "'unknown', now() - interval '30 minutes')",
            d=draft,
            m=mailbox,
            rfc=f"<{uuid4()}@seweb.example>",
        )
    sql(tenant, "UPDATE due_jobs SET due_at = now() - interval '5 minutes' WHERE kind = 'retention.purge'")

    response = client.get("/ops/metrics", headers={"Authorization": "Bearer monitoring-token-for-tests-0001"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    figures = {line.split()[0]: float(line.split()[1]) for line in response.text.strip().splitlines()}
    assert figures["crm_sends_unknown"] == 2  # both workspaces, as a total
    assert 1700 < figures["crm_sends_oldest_unknown_seconds"] < 1900
    assert figures["crm_mailboxes_sync_stale"] == 2 and figures["crm_mailbox_watches_expiring_24h"] == 2
    assert figures["crm_due_jobs_due"] >= 1 and figures["crm_due_jobs_oldest_due_seconds"] >= 290
    assert {"crm_webhook_endpoints_failing", "crm_research_runs_paused", "crm_regulatory_sources_expired"} <= set(
        figures
    )
    # Counts only: nothing that identifies a workspace, a person or a message.
    for private in (
        "private.person",
        "Private subject",
        "seweb.example",
        str(tenant),
        str(other_tenant),
        "Another customer",
    ):
        assert private not in response.text
    # The switch the snapshot uses gives the application's own database role nothing.
    with session_scope(RlsContext(tenant_id=tenant)) as db:
        db.execute(text("SELECT set_config('app.ops_snapshot', 'on', true)"))
        assert db.execute(text("SELECT count(*) FROM send_intents")).scalar() == 1
        assert db.execute(text("SELECT count(*) FROM mailboxes")).scalar() == 1


def test_support_access_without_communications_is_shown_no_message_text(
    owner: TestClient, tenant: UUID, make_client: Any
) -> None:
    """Not only which routes open, but what the open ones return."""
    row = lead_row("S-001", "Salon Aurora", **{"Public business email": "office@example-salon.bg"})
    import_and_commit(owner, workbook_bytes([row]))
    lead = ok(owner.get(f"{API}/leads"))["items"][0]
    mailbox, thread = uuid4(), uuid4()
    sql(
        tenant,
        "INSERT INTO mailboxes (id, tenant_id, provider, email_address, mode, status) "
        "VALUES (:m, :t, 'gmail', 'sales@seweb.example', 'internal', 'active')",
        m=mailbox,
    )
    sql(
        tenant,
        "INSERT INTO email_threads (id, tenant_id, mailbox_id, subject, lead_id, company_id, link_state, has_inbound) "
        "VALUES (:th, :t, :m, 'CANARY-SUBJECT', :l, :c, 'linked', true)",
        th=thread,
        m=mailbox,
        l=lead["id"],
        c=lead["company_id"],
    )
    sql(
        tenant,
        "INSERT INTO email_messages (tenant_id, mailbox_id, thread_id, provider_message_id, direction, classification, subject, "
        "snippet, body_text, sent_at) VALUES (:t, :m, :th, 'c1', 'inbound', 'reply', 'CANARY-SUBJECT', 'CANARY-SNIPPET', "
        "'CANARY-BODY', now())",
        m=mailbox,
        th=thread,
    )
    ok(
        owner.post(
            f"{API}/email-drafts",
            json={"lead_id": lead["id"], "subject": "CANARY-DRAFT", "body_text": "CANARY-DRAFT-BODY"},
        ),
        201,
    )
    ok(owner.post(f"{API}/notes", json={"body": "CANARY-NOTE about what they said", "lead_id": lead["id"]}), 201)
    ok(owner.post(f"{API}/email-suppressions", json={"value": "canary-suppressed@example.bg"}), 201)
    deal = ok(owner.post(f"{API}/deals", json={"company_id": lead["company_id"], "title": "Booking"}), 201)

    support = make_client()
    sign_in(support, "other@example.test")
    create_workspace(support, "Support staff home")
    grant = {"grantee_email": "other@example.test", "hours": 1, "reason": "Checking a display problem"}
    ok(owner.post(f"{API}/support-grants", json=grant), 201)
    assert support.post(f"{API}/auth/switch-tenant", json={"tenant_id": str(tenant)}).status_code == 204

    seen = ""
    for path, params in (
        (f"/prospects/{lead['id']}", {}),
        ("/prospects", {}),
        (f"/leads/{lead['id']}", {}),
        ("/leads", {}),
        (f"/companies/{lead['company_id']}", {}),
        ("/companies", {}),
        (f"/deals/{deal['id']}", {}),
        ("/deals", {}),
        ("/tasks", {}),
        ("/call-queue", {}),
        ("/reports/funnel", {}),
        ("/tenant", {}),
        ("/entitlements", {}),
    ):
        response = support.get(f"{API}{path}", params=params)
        assert response.status_code == 200, (path, response.status_code)
        seen += response.text
    assert "Salon Aurora" in seen  # the records themselves are readable
    for canary in (
        "CANARY-SUBJECT",
        "CANARY-SNIPPET",
        "CANARY-BODY",
        "CANARY-DRAFT",
        "CANARY-NOTE",
        "canary-suppressed",
    ):
        assert canary not in seen, canary
    # The same records with communications included do show the conversation.
    ok(owner.post(f"{API}/support-grants", json={**grant, "include_communications": True}), 201)
    threads = support.get(f"{API}/email-threads", params={"lead_id": lead["id"]})
    assert threads.status_code == 200 and "CANARY-BODY" in threads.text
