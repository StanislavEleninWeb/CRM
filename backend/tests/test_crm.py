"""Core CRM: records, relationships, history, restrictions, merging, files and isolation."""

import io
import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.core.normalize import domain_of, normalize_email, normalize_phone, normalize_url
from app.core.storage import MAX_UPLOAD_BYTES, safe_filename
from tests.helpers import API, create_workspace, join, sign_in

OWNER, MANAGER, REP, VIEWER, OTHER = (
    "owner@example.test",
    "manager@example.test",
    "rep@example.test",
    "viewer@example.test",
    "other@example.test",
)


@pytest.fixture
def owner(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, OWNER)
    create_workspace(client, "SEWEB")
    return client


@pytest.fixture
def rep(owner: TestClient, make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, REP)
    join(owner, client, REP, "representative")
    return client


@pytest.fixture
def viewer(owner: TestClient, make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, VIEWER)
    join(owner, client, VIEWER, "read_only")
    return client


@pytest.fixture
def outsider(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, OTHER)
    create_workspace(client, "SEWEB")  # same name, different tenant
    return client


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def company(client: TestClient, name: str = "Salon Aurora", **fields: Any) -> dict[str, Any]:
    body = {"name": name, "city": "Sofia", "country": "Bulgaria", **fields}
    return ok(client.post(f"{API}/companies", json=body), 201)  # type: ignore[no-any-return]


def channel(client: TestClient, company_id: str, kind: str, value: str, **fields: Any) -> dict[str, Any]:
    body = {"kind": kind, "raw_value": value, **fields}
    return ok(client.post(f"{API}/companies/{company_id}/channels", json=body), 201)  # type: ignore[no-any-return]


def user_id(client: TestClient) -> str:
    return str(client.get(f"{API}/auth/me").json()["user"]["id"])


# --- normalisation ---------------------------------------------------------------------------


def test_phone_normalisation_never_invents_a_country_code() -> None:
    assert normalize_phone("+359 2 900 0001") == ("+35929000001", True)
    assert normalize_phone("0888 123 456", "Bulgaria") == ("+359888123456", True)
    assert normalize_phone("00359 888 123 456") == ("+359888123456", True)
    assert normalize_phone("0888 123 456") == ("0888123456", False)  # country unknown
    assert normalize_phone("0888 123 456", "Atlantis") == ("0888123456", False)
    assert normalize_phone("call us") == (None, False)
    assert normalize_phone("12") == (None, False)


def test_email_and_url_normalisation() -> None:
    assert normalize_email("  Info@Example.BG ") == "info@example.bg"
    assert normalize_email("not found") is None
    assert normalize_email("a@b") is None
    assert normalize_url("Example.BG/contact") == "https://example.bg/contact"
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("ftp://example.bg") is None
    assert domain_of("https://www.Example.bg/x?y=1") == "example.bg"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\evil\x00.pdf") == "evil_.pdf"


# --- the representative's main journey -------------------------------------------------------


def test_representative_journey_from_company_to_won_opportunity(rep: TestClient) -> None:
    acme = company(rep, website_url="www.Salon-Aurora.bg", business_type="Hair salon")
    assert acme["domain"] == "salon-aurora.bg" and acme["website_url"] == "https://www.salon-aurora.bg/"
    contact = ok(rep.post(f"{API}/companies/{acme['id']}/contacts", json={"full_name": "Maria Ivanova"}), 201)
    channel(rep, acme["id"], "phone", "0888 123 456", contact_id=contact["id"])
    channel(rep, acme["id"], "email", "Office@Salon-Aurora.bg")

    lead = ok(rep.post(f"{API}/leads", json={"company_id": acme["id"], "external_id": "SW-900"}), 201)
    assert lead["status"] == "discovered" and lead["company_name"] == "Salon Aurora"
    ok(rep.patch(f"{API}/leads/{lead['id']}", json={"status": "qualified"}))

    deal = ok(rep.post(f"{API}/leads/{lead['id']}/convert", json={"title": "Online booking", "amount": "1500.00"}), 201)
    assert deal["stage_name"] == "New" and deal["stage_kind"] == "open"
    assert deal["amount"] == "1500.00" and deal["currency"] == "EUR"  # tenant default
    assert ok(rep.get(f"{API}/leads/{lead['id']}"))["status"] == "converted"
    assert rep.post(f"{API}/leads/{lead['id']}/convert", json={"title": "Again"}).status_code == 409

    stages = {s["name"]: s for s in ok(rep.get(f"{API}/pipelines"))[0]["stages"]}
    ok(
        rep.patch(
            f"{API}/deals/{deal['id']}",
            json={
                "stage_id": stages["Proposal sent"]["id"],
                "next_action": "Call about proposal",
                "next_action_at": "2026-10-12T09:00:00Z",
            },
        )
    )
    lost = rep.patch(f"{API}/deals/{deal['id']}", json={"stage_id": stages["Lost"]["id"]})
    assert lost.status_code == 422  # a reason is required
    won = ok(rep.patch(f"{API}/deals/{deal['id']}", json={"stage_id": stages["Won"]["id"]}))
    assert won["stage_kind"] == "won" and won["closed_at"] is not None

    history = ok(rep.get(f"{API}/deals/{deal['id']}/stage-history"))
    assert [h["to_stage_name"] for h in history] == ["New", "Proposal sent", "Won"]

    note = ok(rep.post(f"{API}/notes", json={"company_id": acme["id"], "body": "Prefers calls after 14:00"}), 201)
    task = ok(
        rep.post(f"{API}/tasks", json={"company_id": acme["id"], "title": "Send summary", "kind": "follow_up"}), 201
    )
    assert task["assignee_user_id"] == user_id(rep)
    ok(rep.patch(f"{API}/tasks/{task['id']}", json={"status": "done"}))
    assert ok(rep.get(f"{API}/tasks", params={"mine": True}))["total"] == 0
    assert ok(rep.put(f"{API}/company/{acme['id']}/tags", json={"tags": ["VIP", "vip", " Booking "]})) == [
        "Booking",
        "VIP",
    ]

    kinds = [a["kind"] for a in ok(rep.get(f"{API}/activities", params={"company_id": acme["id"]}))["items"]]
    for expected in (
        "company.created",
        "contact.created",
        "lead.created",
        "lead.status_changed",
        "lead.converted",
        "deal.created",
        "deal.stage_changed",
        "note.added",
        "task.completed",
    ):
        assert expected in kinds, expected
    detail = ok(rep.get(f"{API}/companies/{acme['id']}"))
    assert detail["lead_count"] == 1 and detail["deal_count"] == 1 and detail["open_task_count"] == 0
    assert [c["full_name"] for c in detail["contacts"]] == ["Maria Ivanova"]
    assert ok(rep.get(f"{API}/notes", params={"company_id": acme["id"]}))["items"][0]["id"] == note["id"]


def test_a_lead_does_not_need_a_named_person(rep: TestClient) -> None:
    acme = company(rep)
    lead = ok(rep.post(f"{API}/leads", json={"company_id": acme["id"]}), 201)
    assert lead["contact_id"] is None
    assert rep.patch(f"{API}/leads/{lead['id']}", json={"status": "disqualified"}).status_code == 422
    done = ok(rep.patch(f"{API}/leads/{lead['id']}", json={"status": "disqualified", "disqualify_reason": "Closed"}))
    assert done["disqualify_reason"] == "Closed"
    assert rep.post(f"{API}/leads/{lead['id']}/convert", json={"title": "X"}).status_code == 409
    assert rep.post(f"{API}/leads", json={"company_id": acme["id"], "external_id": "SW-1"}).status_code == 201
    assert rep.post(f"{API}/leads", json={"company_id": acme["id"], "external_id": "SW-1"}).status_code == 409


def test_money_keeps_currency_and_conversion_provenance(rep: TestClient) -> None:
    acme = company(rep)
    incomplete = rep.post(
        f"{API}/deals",
        json={"company_id": acme["id"], "title": "Legacy", "amount": "511.29", "original_amount": "1000.00"},
    )
    assert incomplete.status_code == 422
    converted = ok(
        rep.post(
            f"{API}/deals",
            json={
                "company_id": acme["id"],
                "title": "Legacy",
                "amount": "511.29",
                "currency": "EUR",
                "original_amount": "1000.00",
                "original_currency": "BGN",
                "conversion_rate": "1.95583",
                "conversion_source": "Fixed euro changeover rate",
                "conversion_date": "2026-01-01",
            },
        ),
        201,
    )
    assert converted["original_currency"] == "BGN" and converted["conversion_rate"] == "1.95583000"
    no_amount = ok(rep.post(f"{API}/deals", json={"company_id": acme["id"], "title": "Unpriced"}), 201)
    assert no_amount["amount"] is None and no_amount["currency"] is None
    assert rep.post(f"{API}/deals", json={"company_id": acme["id"], "title": "Bad", "amount": "-1"}).status_code == 422
    assert (
        rep.post(f"{API}/deals", json={"company_id": acme["id"], "title": "Bad", "amount": "1.999"}).status_code == 422
    )


# --- channels and restrictions ---------------------------------------------------------------


def test_phone_purpose_and_emergency_numbers(rep: TestClient) -> None:
    clinic = company(rep, "Vet Clinic")
    general = channel(rep, clinic["id"], "phone", "0887 000 003")
    emergency = channel(rep, clinic["id"], "phone", "0884 000 004", purpose="emergency", label="emergency")
    delivery = channel(rep, clinic["id"], "phone", "+359 888 00 00 06", purpose="delivery")
    assert general["normalized_value"] == "+359887000003" and general["dial_uri"] == "tel:+359887000003"
    assert emergency["allow_sales_use"] is False and emergency["dial_uri"] is None
    assert delivery["purpose"] == "delivery" and delivery["dial_uri"] == "tel:+359888000006"
    assert emergency["raw_value"] == "0884 000 004"  # the original text is kept

    unknown_country = company(rep, "No Country", country=None)
    national = channel(rep, unknown_country["id"], "phone", "0888 123 456")
    assert national["normalized_value"] == "0888123456" and national["normalized_is_e164"] is False
    assert (
        rep.post(f"{API}/companies/{clinic['id']}/channels", json={"kind": "email", "raw_value": "nope"}).status_code
        == 422
    )
    assert (
        rep.post(f"{API}/companies/{clinic['id']}/channels", json={"kind": "phone", "raw_value": "abc"}).status_code
        == 422
    )


def test_do_not_contact_needs_a_reason_and_only_a_manager_can_lift_it(owner: TestClient, rep: TestClient) -> None:
    acme = company(rep)
    phone = channel(rep, acme["id"], "phone", "0888 123 456")
    assert rep.patch(f"{API}/channels/{phone['id']}", json={"do_not_contact": True}).status_code == 422
    blocked = ok(
        rep.patch(
            f"{API}/channels/{phone['id']}",
            json={"do_not_contact": True, "restriction_reason": "Asked not to be called"},
        )
    )
    assert blocked["dial_uri"] is None and blocked["restriction_reason"] == "Asked not to be called"
    assert rep.delete(f"{API}/channels/{phone['id']}").status_code == 409  # cannot erase a restriction
    assert rep.patch(f"{API}/channels/{phone['id']}", json={"do_not_contact": False}).status_code == 403
    lifted = ok(owner.patch(f"{API}/channels/{phone['id']}", json={"do_not_contact": False}))
    assert lifted["dial_uri"] == "tel:+359888123456"
    actions = [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]
    assert "channel.restricted" in actions and "channel.restriction_lifted" in actions


def test_company_level_restriction_blocks_every_number(owner: TestClient, rep: TestClient) -> None:
    acme = company(rep)
    channel(rep, acme["id"], "phone", "0888 123 456")
    channel(rep, acme["id"], "phone", "02 900 0001")
    channel(rep, acme["id"], "email", "office@acme.example.bg")
    restriction = ok(
        rep.post(
            f"{API}/companies/{acme['id']}/restrictions",
            json={"channel_kind": "phone", "reason": "Owner asked: no calls"},
        ),
        201,
    )
    detail = ok(rep.get(f"{API}/companies/{acme['id']}"))
    phones = [c for c in detail["channels"] if c["kind"] == "phone"]
    assert len(phones) == 2 and all(c["dial_uri"] is None and not c["allow_sales_use"] for c in phones)
    assert next(c for c in detail["channels"] if c["kind"] == "email")["allow_sales_use"] is True
    # A number added later is covered too.
    assert channel(rep, acme["id"], "phone", "0899 000 111")["dial_uri"] is None
    assert rep.post(f"{API}/restrictions/{restriction['id']}/lift", json={"lift_reason": "x"}).status_code == 403
    ok(owner.post(f"{API}/restrictions/{restriction['id']}/lift", json={"lift_reason": "Customer called us back"}))
    assert owner.post(f"{API}/restrictions/{restriction['id']}/lift", json={"lift_reason": "again"}).status_code == 404
    detail = ok(rep.get(f"{API}/companies/{acme['id']}"))
    assert all(c["dial_uri"] for c in detail["channels"] if c["kind"] == "phone")
    assert detail["restrictions"][0]["lift_reason"] == "Customer called us back"  # history is kept


# --- permissions -----------------------------------------------------------------------------


def test_read_only_members_can_read_but_not_change_crm_records(rep: TestClient, viewer: TestClient) -> None:
    acme = company(rep)
    assert ok(viewer.get(f"{API}/companies"))["total"] == 1
    assert ok(viewer.get(f"{API}/companies/{acme['id']}"))["name"] == "Salon Aurora"
    for method, path, body in (
        ("POST", "/companies", {"name": "X"}),
        ("PATCH", f"/companies/{acme['id']}", {"name": "X"}),
        ("DELETE", f"/companies/{acme['id']}", None),
        ("POST", f"/companies/{acme['id']}/channels", {"kind": "phone", "raw_value": "0888 123 456"}),
        ("POST", "/leads", {"company_id": acme["id"]}),
        ("POST", "/deals", {"company_id": acme["id"], "title": "X"}),
        ("POST", "/tasks", {"company_id": acme["id"], "title": "X"}),
        ("POST", "/notes", {"company_id": acme["id"], "body": "X"}),
        ("PUT", f"/company/{acme['id']}/tags", {"tags": ["x"]}),
        ("POST", "/companies/bulk", {"ids": [acme["id"]], "action": "archive", "dry_run": False}),
        ("POST", "/pipelines", {"name": "X"}),
    ):
        assert viewer.request(method, f"{API}{path}", json=body).status_code == 403, path
    assert ok(viewer.get(f"{API}/companies/{acme['id']}"))["name"] == "Salon Aurora"


def test_representative_limits(owner: TestClient, rep: TestClient) -> None:
    acme = company(rep)
    assert rep.delete(f"{API}/companies/{acme['id']}").status_code == 403
    assert rep.post(f"{API}/companies/bulk", json={"ids": [acme["id"]], "action": "archive"}).status_code == 403
    assert rep.post(f"{API}/pipelines", json={"name": "Partners"}).status_code == 403
    assert (
        rep.post(
            f"{API}/custom-fields", json={"entity_type": "company", "key": "x", "label": "X", "field_type": "text"}
        ).status_code
        == 403
    )
    # A representative may take a record but not hand it to someone else.
    assert rep.patch(f"{API}/companies/{acme['id']}", json={"owner_user_id": user_id(rep)}).status_code == 200
    assert rep.patch(f"{API}/companies/{acme['id']}", json={"owner_user_id": user_id(owner)}).status_code == 403
    assert owner.patch(f"{API}/companies/{acme['id']}", json={"owner_user_id": user_id(owner)}).status_code == 200
    assert owner.patch(f"{API}/companies/{acme['id']}", json={"owner_user_id": str(uuid4())}).status_code == 422


def test_bulk_actions_preview_first_and_report_missing(owner: TestClient) -> None:
    ids = [company(owner, f"Company {i}")["id"] for i in range(5)]
    ghost = str(uuid4())
    preview = ok(owner.post(f"{API}/companies/bulk", json={"ids": [*ids, ghost], "action": "archive"}))
    assert preview == {"matched": 5, "changed": 0, "dry_run": True, "missing": [ghost]}
    assert ok(owner.get(f"{API}/companies"))["total"] == 5
    applied = ok(owner.post(f"{API}/companies/bulk", json={"ids": ids[:3], "action": "archive", "dry_run": False}))
    assert applied["changed"] == 3
    assert ok(owner.get(f"{API}/companies"))["total"] == 2
    assert ok(owner.get(f"{API}/companies", params={"archived": True}))["total"] == 3
    ok(owner.post(f"{API}/companies/bulk", json={"ids": ids[:3], "action": "unarchive", "dry_run": False}))  # undo
    assert ok(owner.get(f"{API}/companies"))["total"] == 5
    tagged = ok(
        owner.post(f"{API}/companies/bulk", json={"ids": ids, "action": "add_tag", "tag": "Q4", "dry_run": False})
    )
    assert tagged["changed"] == 5
    assert ok(owner.get(f"{API}/companies", params={"tag": "q4"}))["total"] == 5


# --- lists -----------------------------------------------------------------------------------


def test_pagination_filtering_and_safe_sorting(owner: TestClient, migrator_engine: Any) -> None:
    tenant_id = ok(owner.get(f"{API}/tenant"))["id"]
    with migrator_engine.begin() as conn:  # 230 rows, seeded directly for speed
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        conn.execute(
            text(
                "INSERT INTO companies (tenant_id, name, city, industry_group) "
                "SELECT :t, 'Company ' || lpad(n::text, 3, '0'), "
                "CASE WHEN n % 2 = 0 THEN 'Sofia' ELSE 'Varna' END, "
                "CASE WHEN n % 5 = 0 THEN 'Veterinary' ELSE 'Restaurants' END "
                "FROM generate_series(1, 230) AS n"
            ),
            {"t": tenant_id},
        )
    first = ok(owner.get(f"{API}/companies", params={"limit": 100}))
    assert first["total"] == 230 and len(first["items"]) == 100
    seen = {c["id"] for c in first["items"]}
    for offset in (100, 200):
        page = ok(owner.get(f"{API}/companies", params={"limit": 100, "offset": offset}))
        assert not seen & {c["id"] for c in page["items"]}
        seen |= {c["id"] for c in page["items"]}
    assert len(seen) == 230
    assert ok(owner.get(f"{API}/companies", params={"city": "sofia"}))["total"] == 115
    assert ok(owner.get(f"{API}/companies", params={"city": "Sofia", "industry_group": "Veterinary"}))["total"] == 23
    assert ok(owner.get(f"{API}/companies", params={"q": "company 01"}))["total"] == 10
    assert ok(owner.get(f"{API}/companies", params={"q": "100%"}))["total"] == 0  # wildcards are literal
    newest = ok(owner.get(f"{API}/companies", params={"sort": "-name", "limit": 1}))["items"][0]["name"]
    assert newest == "Company 230"
    assert owner.get(f"{API}/companies", params={"limit": 201}).status_code == 422
    bad_sort = owner.get(f"{API}/companies", params={"sort": "name; DROP TABLE companies"})
    assert bad_sort.status_code == 422
    assert ok(owner.get(f"{API}/companies"))["total"] == 230


def test_saved_views_are_private_unless_shared(owner: TestClient, rep: TestClient) -> None:
    mine = ok(
        rep.post(
            f"{API}/saved-views", json={"entity_type": "company", "name": "Sofia vets", "filters": {"city": "Sofia"}}
        ),
        201,
    )
    assert (
        rep.post(f"{API}/saved-views", json={"entity_type": "company", "name": "Team", "is_shared": True}).status_code
        == 403
    )
    shared = ok(
        owner.post(f"{API}/saved-views", json={"entity_type": "company", "name": "Team", "is_shared": True}), 201
    )
    assert [v["name"] for v in ok(rep.get(f"{API}/saved-views", params={"entity_type": "company"}))] == [
        "Sofia vets",
        "Team",
    ]
    assert [v["name"] for v in ok(owner.get(f"{API}/saved-views", params={"entity_type": "company"}))] == ["Team"]
    assert rep.delete(f"{API}/saved-views/{shared['id']}").status_code == 403
    assert rep.delete(f"{API}/saved-views/{mine['id']}").status_code == 204


def test_custom_fields_are_bounded_and_validated(owner: TestClient) -> None:
    ok(
        owner.post(
            f"{API}/custom-fields",
            json={"entity_type": "company", "key": "employees", "label": "Employees", "field_type": "number"},
        ),
        201,
    )
    ok(
        owner.post(
            f"{API}/custom-fields",
            json={
                "entity_type": "company",
                "key": "size",
                "label": "Size",
                "field_type": "select",
                "options": ["S", "M"],
            },
        ),
        201,
    )
    assert (
        owner.post(
            f"{API}/custom-fields", json={"entity_type": "company", "key": "size", "label": "Dup", "field_type": "text"}
        ).status_code
        == 409
    )
    good = company(owner, custom={"employees": 12, "size": "M"})
    assert good["custom"] == {"employees": 12, "size": "M"}
    assert owner.post(f"{API}/companies", json={"name": "X", "custom": {"unknown": 1}}).status_code == 422
    assert owner.post(f"{API}/companies", json={"name": "X", "custom": {"employees": "many"}}).status_code == 422
    assert owner.post(f"{API}/companies", json={"name": "X", "custom": {"size": "XL"}}).status_code == 422
    assert owner.post(f"{API}/companies", json={"name": "X", "unexpected_field": 1}).status_code == 422


def test_pipeline_stages_are_customisable_but_protected(owner: TestClient) -> None:
    pipeline = ok(owner.get(f"{API}/pipelines"))[0]
    assert [s["name"] for s in pipeline["stages"]] == [
        "New",
        "Contacted",
        "Replied",
        "Meeting booked",
        "Proposal sent",
        "Negotiation",
        "Won",
        "Lost",
    ]
    updated = ok(owner.post(f"{API}/pipelines/{pipeline['id']}/stages", json={"name": "Demo", "position": 35}), 201)
    assert [s["name"] for s in updated["stages"]][:5] == ["New", "Contacted", "Replied", "Demo", "Meeting booked"]
    demo = next(s for s in updated["stages"] if s["name"] == "Demo")
    acme = company(owner)
    deal = ok(owner.post(f"{API}/deals", json={"company_id": acme["id"], "title": "D", "stage_id": demo["id"]}), 201)
    assert owner.delete(f"{API}/stages/{demo['id']}").status_code == 409  # in use
    won = next(s for s in updated["stages"] if s["kind"] == "won")
    assert owner.delete(f"{API}/stages/{won['id']}").status_code == 409  # last won stage
    other = ok(owner.post(f"{API}/pipelines", json={"name": "Partners"}), 201)
    wrong = owner.patch(f"{API}/deals/{deal['id']}", json={"stage_id": other["stages"][0]["id"]})
    assert wrong.status_code == 404  # a stage from another pipeline


# --- duplicates and merging ------------------------------------------------------------------


def test_duplicate_suggestions_do_not_treat_branches_as_duplicates(rep: TestClient) -> None:
    sofia = company(rep, "Happy Dental", website_url="happydental.bg", city="Sofia")
    varna = company(rep, "Happy Dental Varna", website_url="happydental.bg/varna", city="Varna")
    twin = company(rep, "Happy Dental Ltd", city="Sofia")
    channel(rep, sofia["id"], "phone", "0888 123 456")
    channel(rep, twin["id"], "phone", "+359 888 123 456")
    company(rep, "Unrelated Bakery", city="Sofia")
    suggestions = {s["company"]["id"]: s for s in ok(rep.get(f"{API}/companies/{sofia['id']}/duplicates"))}
    assert set(suggestions) == {varna["id"], twin["id"]}
    assert suggestions[varna["id"]]["reasons"] == ["Same website domain"]
    assert "branch" in suggestions[varna["id"]]["caution"]
    assert "Shares a phone number" in suggestions[twin["id"]]["reasons"]
    assert suggestions[twin["id"]]["caution"] is None
    assert ok(rep.get(f"{API}/companies"))["total"] == 4  # nothing was merged


def test_merging_preserves_history_relationships_and_restrictions(owner: TestClient, rep: TestClient) -> None:
    keep = company(owner, "Happy Dental", city=None)
    dup = company(owner, "Happy Dental Ltd", city="Sofia", website_url="happydental.bg")
    channel(owner, keep["id"], "phone", "0888 123 456")
    shared = channel(owner, dup["id"], "phone", "+359 888 123 456")
    channel(owner, dup["id"], "email", "office@happydental.bg")
    ok(owner.patch(f"{API}/channels/{shared['id']}", json={"do_not_contact": True, "restriction_reason": "Opted out"}))
    ok(
        owner.post(
            f"{API}/companies/{dup['id']}/restrictions", json={"channel_kind": "email", "reason": "No marketing email"}
        ),
        201,
    )
    contact = ok(owner.post(f"{API}/companies/{dup['id']}/contacts", json={"full_name": "Dr Petrov"}), 201)
    lead = ok(owner.post(f"{API}/leads", json={"company_id": dup["id"], "external_id": "SW-77"}), 201)
    deal = ok(owner.post(f"{API}/deals", json={"company_id": dup["id"], "title": "Website"}), 201)
    task = ok(
        owner.post(f"{API}/tasks", json={"company_id": dup["id"], "deal_id": deal["id"], "title": "Call back"}), 201
    )
    ok(owner.post(f"{API}/notes", json={"company_id": dup["id"], "body": "Met at the expo"}), 201)
    ok(owner.put(f"{API}/company/{dup['id']}/tags", json={"tags": ["Dental"]}))
    upload = owner.post(
        f"{API}/attachments",
        data={"company_id": dup["id"]},
        files={"file": ("brief.pdf", b"%PDF-1.4 brief", "application/pdf")},
    )
    attachment = ok(upload, 201)

    assert (
        rep.post(f"{API}/companies/{keep['id']}/merge", json={"source_id": dup["id"], "confirm": True}).status_code
        == 403
    )
    assert owner.post(f"{API}/companies/{keep['id']}/merge", json={"source_id": dup["id"]}).status_code == 422
    assert (
        owner.post(f"{API}/companies/{keep['id']}/merge", json={"source_id": keep["id"], "confirm": True}).status_code
        == 422
    )
    merged = ok(owner.post(f"{API}/companies/{keep['id']}/merge", json={"source_id": dup["id"], "confirm": True}))

    assert merged["city"] == "Sofia" and merged["domain"] == "happydental.bg"  # blanks filled
    assert merged["name"] == "Happy Dental"  # existing values kept
    assert merged["lead_count"] == 1 and merged["deal_count"] == 1 and merged["open_task_count"] == 1
    assert [c["id"] for c in merged["contacts"]] == [contact["id"]]
    assert merged["tags"] == ["Dental"]
    phones = [c for c in merged["channels"] if c["kind"] == "phone"]
    assert len(phones) == 1 and phones[0]["do_not_contact"] and phones[0]["restriction_reason"] == "Opted out"
    assert phones[0]["dial_uri"] is None
    email = next(c for c in merged["channels"] if c["kind"] == "email")
    assert email["allow_sales_use"] is False  # the email restriction moved with the record
    assert [r["reason"] for r in merged["restrictions"]] == ["No marketing email"]

    assert ok(owner.get(f"{API}/leads/{lead['id']}"))["company_id"] == keep["id"]
    assert ok(owner.get(f"{API}/deals/{deal['id']}"))["company_id"] == keep["id"]
    assert ok(owner.get(f"{API}/tasks", params={"company_id": keep["id"]}))["items"][0]["id"] == task["id"]
    assert ok(owner.get(f"{API}/notes", params={"company_id": keep["id"]}))["total"] == 1
    assert [a["id"] for a in ok(owner.get(f"{API}/attachments", params={"company_id": keep["id"]}))] == [
        attachment["id"]
    ]
    timeline = [
        a["kind"] for a in ok(owner.get(f"{API}/activities", params={"company_id": keep["id"], "limit": 200}))["items"]
    ]
    assert "company.merged" in timeline and "note.added" in timeline and "lead.created" in timeline
    assert ok(owner.get(f"{API}/activities", params={"company_id": dup["id"]}))["total"] == 0

    assert ok(owner.get(f"{API}/companies"))["total"] == 1
    gone = ok(owner.get(f"{API}/companies/{dup['id']}"))
    assert gone["merged_into_id"] == keep["id"] and gone["archived_at"] is not None
    again = owner.post(f"{API}/companies/{keep['id']}/merge", json={"source_id": dup["id"], "confirm": True})
    assert again.status_code == 409


# --- files -----------------------------------------------------------------------------------


def test_file_upload_limits_and_download(rep: TestClient, viewer: TestClient) -> None:
    acme = company(rep)
    stored = ok(
        rep.post(
            f"{API}/attachments",
            data={"company_id": acme["id"]},
            files={"file": ("../../secret/Оферта 2026.pdf", b"%PDF-1.4 hello", "application/pdf")},
        ),
        201,
    )
    assert stored["filename"] == "Оферта 2026.pdf" and stored["size_bytes"] == 14
    download = viewer.get(f"{API}/attachments/{stored['id']}/download")
    assert download.status_code == 200 and download.content == b"%PDF-1.4 hello"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "no-store" in download.headers["cache-control"]

    def upload(name: str, data: bytes, content_type: str, **form: str) -> Any:
        return rep.post(
            f"{API}/attachments",
            data=form or {"company_id": acme["id"]},
            files={"file": (name, io.BytesIO(data), content_type)},
        )

    assert upload("page.html", b"<script>alert(1)</script>", "text/html").status_code == 422
    assert upload("run.exe", b"MZ", "application/x-msdownload").status_code == 422
    assert upload("empty.pdf", b"", "application/pdf").status_code == 422
    assert upload("big.pdf", b"x" * (MAX_UPLOAD_BYTES + 1), "application/pdf").status_code == 413
    assert upload("orphan.pdf", b"x", "application/pdf", lead_id="").status_code == 422
    assert rep.delete(f"{API}/attachments/{stored['id']}").status_code == 204
    assert viewer.get(f"{API}/attachments/{stored['id']}/download").status_code == 404


# --- isolation -------------------------------------------------------------------------------


def test_crm_records_are_invisible_and_unlinkable_across_tenants(rep: TestClient, outsider: TestClient) -> None:
    acme = company(rep)
    phone = channel(rep, acme["id"], "phone", "0888 123 456")
    lead = ok(rep.post(f"{API}/leads", json={"company_id": acme["id"]}), 201)
    deal = ok(rep.post(f"{API}/deals", json={"company_id": acme["id"], "title": "Site"}), 201)
    task = ok(rep.post(f"{API}/tasks", json={"company_id": acme["id"], "title": "T"}), 201)
    stage = ok(rep.get(f"{API}/pipelines"))[0]["stages"][1]
    stored = ok(
        rep.post(
            f"{API}/attachments",
            data={"company_id": acme["id"]},
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        ),
        201,
    )
    mine = company(outsider, "Outsider Co")

    for path in (
        f"/companies/{acme['id']}",
        f"/leads/{lead['id']}",
        f"/deals/{deal['id']}",
        f"/deals/{deal['id']}/stage-history",
        f"/companies/{acme['id']}/duplicates",
        f"/attachments/{stored['id']}/download",
    ):
        assert outsider.get(f"{API}{path}").status_code == 404, path
    for path, params in (
        ("/companies", {}),
        ("/leads", {}),
        ("/deals", {}),
        ("/tasks", {}),
        ("/activities", {"company_id": acme["id"]}),
        ("/notes", {"company_id": acme["id"]}),
    ):
        assert ok(outsider.get(f"{API}{path}", params={**params}))["total"] == (1 if path == "/companies" else 0), path
    assert outsider.get(f"{API}/attachments", params={"company_id": acme["id"]}).json() == []

    for method, path, body in (
        ("PATCH", f"/companies/{acme['id']}", {"name": "Hacked"}),
        ("DELETE", f"/companies/{acme['id']}", None),
        ("PATCH", f"/channels/{phone['id']}", {"label": "x"}),
        ("DELETE", f"/channels/{phone['id']}", None),
        ("PATCH", f"/leads/{lead['id']}", {"status": "qualified"}),
        ("PATCH", f"/deals/{deal['id']}", {"title": "Hacked"}),
        ("PATCH", f"/tasks/{task['id']}", {"status": "done"}),
        ("DELETE", f"/attachments/{stored['id']}", None),
        ("POST", f"/companies/{acme['id']}/contacts", {"full_name": "X"}),
        ("POST", f"/companies/{acme['id']}/channels", {"kind": "phone", "raw_value": "0888 000 000"}),
        ("POST", f"/companies/{acme['id']}/restrictions", {"channel_kind": "any", "reason": "x"}),
        ("POST", f"/companies/{mine['id']}/merge", {"source_id": acme["id"], "confirm": True}),
        ("POST", f"/leads/{lead['id']}/convert", {"title": "X"}),
    ):
        assert outsider.request(method, f"{API}{path}", json=body).status_code == 404, path

    # Linking one's own records to another tenant's records is refused with a clear error.
    for path, body in (
        ("/leads", {"company_id": acme["id"]}),
        ("/deals", {"company_id": acme["id"], "title": "X"}),
        ("/deals", {"company_id": mine["id"], "title": "X", "stage_id": stage["id"]}),
        ("/deals", {"company_id": mine["id"], "title": "X", "lead_id": lead["id"]}),
        ("/tasks", {"company_id": acme["id"], "title": "X"}),
        ("/tasks", {"company_id": mine["id"], "deal_id": deal["id"], "title": "X"}),
        ("/notes", {"company_id": acme["id"], "body": "X"}),
        ("/notes", {"company_id": mine["id"], "lead_id": lead["id"], "body": "X"}),
    ):
        assert outsider.post(f"{API}{path}", json=body).status_code == 422, (path, body)
    bulk = ok(outsider.post(f"{API}/companies/bulk", json={"ids": [acme["id"]], "action": "archive", "dry_run": False}))
    assert bulk["matched"] == 0 and bulk["missing"] == [acme["id"]]
    assert outsider.put(f"{API}/company/{acme['id']}/tags", json={"tags": ["x"]}).status_code == 422
    assert ok(rep.get(f"{API}/companies/{acme['id']}"))["name"] == "Salon Aurora"
    assert ok(rep.get(f"{API}/companies/{acme['id']}"))["archived_at"] is None


@pytest.fixture
def app_engine() -> Iterator[Any]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


def test_composite_foreign_keys_refuse_cross_tenant_links_in_the_database(
    rep: TestClient, outsider: TestClient, app_engine: Any
) -> None:
    acme = company(rep)
    lead = ok(rep.post(f"{API}/leads", json={"company_id": acme["id"]}), 201)
    stage = ok(rep.get(f"{API}/pipelines"))[0]["stages"][0]
    mine = company(outsider, "Outsider Co")
    my_pipeline = ok(outsider.get(f"{API}/pipelines"))[0]
    tenant_b = ok(outsider.get(f"{API}/tenant"))["id"]

    def as_outsider(sql: str, **params: Any) -> None:
        with app_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_b})
            conn.execute(text(sql), {"t": tenant_b, **params})

    attempts = {
        "fk_contacts_company_id": (
            "INSERT INTO contacts (tenant_id, company_id, full_name) VALUES (:t, :c, 'X')",
            {"c": acme["id"]},
        ),
        "fk_contact_channels_company_id": (
            "INSERT INTO contact_channels (tenant_id, company_id, kind, raw_value) VALUES (:t, :c, 'phone', '1')",
            {"c": acme["id"]},
        ),
        "fk_leads_company_id": ("INSERT INTO leads (tenant_id, company_id) VALUES (:t, :c)", {"c": acme["id"]}),
        "fk_deals_stage": (
            "INSERT INTO deals (tenant_id, company_id, pipeline_id, stage_id, title) VALUES (:t, :c, :p, :s, 'X')",
            {"c": mine["id"], "p": my_pipeline["id"], "s": stage["id"]},
        ),
        "fk_deals_lead_id": (
            "INSERT INTO deals (tenant_id, company_id, pipeline_id, stage_id, title, lead_id) VALUES (:t, :c, :p, :s, 'X', :l)",
            {"c": mine["id"], "p": my_pipeline["id"], "s": my_pipeline["stages"][0]["id"], "l": lead["id"]},
        ),
        "fk_tasks_company_id": (
            "INSERT INTO tasks (tenant_id, title, company_id) VALUES (:t, 'X', :c)",
            {"c": acme["id"]},
        ),
        "fk_notes_lead_id": (
            "INSERT INTO notes (tenant_id, body, company_id, lead_id) VALUES (:t, 'X', :c, :l)",
            {"c": mine["id"], "l": lead["id"]},
        ),
        "fk_companies_owner_user_id": (
            "UPDATE companies SET owner_user_id = :u WHERE tenant_id = :t",
            {"u": user_id(rep)},
        ),
        "fk_companies_merged_into_id": (
            "UPDATE companies SET merged_into_id = :c WHERE tenant_id = :t",
            {"c": acme["id"]},
        ),
    }
    for constraint, (sql, params) in attempts.items():
        with pytest.raises(DBAPIError, match=constraint):
            as_outsider(sql, **params)
    with pytest.raises(DBAPIError, match="row-level security"):
        as_outsider(
            "INSERT INTO companies (tenant_id, name) VALUES (:other, 'Planted')",
            other=ok(rep.get(f"{API}/tenant"))["id"],
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        as_outsider("UPDATE activities SET summary = 'rewritten'")
    with pytest.raises(DBAPIError, match="permission denied"):
        as_outsider("DELETE FROM contact_restrictions")


def test_every_tenant_table_has_forced_row_level_security(
    migrator_engine: Any, app_engine: Any, rep: TestClient
) -> None:
    """A guard for future migrations: any table with tenant_id must be isolated."""
    company(rep)
    # ENABLE rather than FORCE, each for a documented reason: the first two are identity tables with
    # definer access (migration 0002); due_jobs is claimed across tenants by the scheduler (migration 0007);
    # mailbox_routes maps an incoming push notification to its tenant (migration 0008).
    identity_tables = {"memberships", "invitations", "due_jobs", "mailbox_routes"}
    with migrator_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies,
                       (SELECT a.attnotnull FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id') AS not_null
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
                """
            )
        ).all()
    assert len(rows) >= 19
    for row in rows:
        assert row.relrowsecurity, row.relname
        assert row.policies >= 1, row.relname
        assert row.not_null, row.relname
        if row.relname not in identity_tables:
            assert row.relforcerowsecurity, row.relname
    with app_engine.connect() as conn:
        for row in rows:
            assert conn.execute(text(f"SELECT count(*) FROM {row.relname}")).scalar() == 0, row.relname
