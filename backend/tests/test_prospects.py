"""Prospect workspace: ranking, filters, verification, daily queue, call outcomes and follow-ups."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.helpers import API, create_workspace, join, sign_in
from tests.test_import import import_and_commit, lead_row, ok, workbook_bytes

SCORES = {
    "evidence": "Score: evidence strength (0–30)",
    "relevance": "Score: relevance to SEWEB (0–25)",
    "value": "Score: business value (0–20)",
    "reachability": "Score: reachability (0–15)",
    "activity": "Score: business activity (0–10)",
}


def row(lead_id: str, name: str, total: int, **overrides: Any) -> dict[str, Any]:
    """A synthetic lead whose five components add up to ``total``."""
    remaining, components = total, {}
    for key, cap in (("evidence", 30), ("relevance", 25), ("value", 20), ("reachability", 15), ("activity", 10)):
        components[SCORES[key]] = min(cap, remaining)
        remaining -= components[SCORES[key]]
    tier = "A" if total >= 80 else "B" if total >= 60 else "C"
    base = {
        "Priority score, 0–100": total,
        "Priority tier: A, B, or C": tier,
        "Recommended contact channel": "Phone",
        "Public business phone": f"0888 {abs(hash(lead_id)) % 900 + 100} {abs(hash(name)) % 900 + 100}",
        **components,
    }
    return lead_row(lead_id, name, **{**base, **overrides})


@pytest.fixture
def owner(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, "owner@example.test")
    create_workspace(client, "SEWEB")
    return client


@pytest.fixture
def rep(owner: TestClient, make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, "rep@example.test")
    join(owner, client, "rep@example.test", "representative")
    return client


def prospects(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    return ok(client.get(f"{API}/prospects", params=params))["items"]  # type: ignore[no-any-return]


def by_id(client: TestClient) -> dict[str, dict[str, Any]]:
    return {p["external_id"]: p for p in prospects(client, limit=200)}


def set_today(engine: Any, client: TestClient, checked_days_ago: int, lead_ids: list[str] | None = None) -> None:
    tenant = ok(client.get(f"{API}/tenant"))["id"]
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        conn.execute(
            text(
                "UPDATE lead_assessments a SET checked_on = current_date - :d FROM leads l WHERE l.id = a.lead_id "
                "AND a.tenant_id = :t AND (CAST(:ids AS text[]) IS NULL OR l.external_id = ANY(:ids))"
            ),
            {"t": tenant, "d": checked_days_ago, "ids": lead_ids},
        )


# --- ranking and filters ---------------------------------------------------------------------


def test_ranking_is_deterministic_and_explained(owner: TestClient, migrator_engine: Any) -> None:
    import_and_commit(
        owner,
        workbook_bytes(
            [
                row("P-003", "Charlie", 85, **{"Evidence confidence: high, medium, or low": "medium"}),
                row("P-001", "Alpha", 85),
                row("P-002", "Bravo", 85),
                row("P-004", "Delta", 91),
                row("P-005", "Echo", 60, City="Varna"),
                row(
                    "P-006",
                    "Unscored",
                    0,
                    **{h: None for h in [*SCORES.values(), "Priority score, 0–100", "Priority tier: A, B, or C"]},
                ),
            ]
        ),
    )
    set_today(migrator_engine, owner, 0)
    ranked = prospects(owner)
    # Score, then confidence (high before medium), then Lead ID; unscored last and shown as null, not 0.
    assert [p["external_id"] for p in ranked] == ["P-004", "P-001", "P-002", "P-003", "P-005", "P-006"]
    assert ranked[-1]["total"] is None and ranked[-1]["tier"] is None
    assert ranked[0]["components"] == {"evidence": 30, "relevance": 25, "value": 20, "reachability": 15, "activity": 1}
    assert [p["external_id"] for p in prospects(owner, tier="A")] == ["P-004", "P-001", "P-002", "P-003"]
    assert [p["external_id"] for p in prospects(owner, tier="unscored")] == ["P-006"]
    assert [p["external_id"] for p in prospects(owner, city="varna")] == ["P-005"]
    assert [p["external_id"] for p in prospects(owner, confidence="medium")] == ["P-003"]
    assert [p["external_id"] for p in prospects(owner, q="alph")] == ["P-001"]
    assert len(prospects(owner, tag="other")) == 6 and prospects(owner, tag="nope") == []
    assert len(prospects(owner, service="online booking system + ai assistant")) == 6
    assert len(prospects(owner, channel="phone")) == 6 and prospects(owner, channel="email") == []
    facets = ok(owner.get(f"{API}/prospects/facets"))
    assert facets["cities"] == ["Sofia", "Varna"] and "Other" in facets["tags"]

    # An edit changes the ranking immediately.
    delta = ranked[0]["lead_id"]
    ok(
        owner.post(
            f"{API}/leads/{delta}/scores",
            json={
                "components": {"evidence": 10, "relevance": 10, "value": 10, "reachability": 10, "activity": 10},
                "reason": "Re-checked",
            },
        ),
        201,
    )
    assert [p["external_id"] for p in prospects(owner)][:4] == ["P-001", "P-002", "P-003", "P-005"]
    assert by_id(owner)["P-004"]["score_origin"] == "override"


def test_stale_and_uncertain_findings_go_to_the_verification_queue(owner: TestClient, migrator_engine: Any) -> None:
    import_and_commit(
        owner,
        workbook_bytes(
            [
                row("P-001", "Tier A unverified", 90),
                row("P-002", "Tier B fine", 70),
                row("P-003", "Low confidence", 70, **{"Evidence confidence: high, medium, or low": "low"}),
                row("P-004", "Old check", 70),
            ]
        ),
    )
    set_today(migrator_engine, owner, 0)
    set_today(migrator_engine, owner, 45, ["P-004"])  # older than the 30-day default
    reasons = {p["external_id"]: p["needs_verification"] for p in prospects(owner)}
    assert reasons == {
        "P-001": ["unverified_high_priority"],  # imported research is not verified research
        "P-002": [],
        "P-003": ["low_confidence"],
        "P-004": ["stale_evidence"],
    }
    assert {p["external_id"] for p in prospects(owner, needs_verification=True)} == {"P-001", "P-003", "P-004"}
    assert [p["external_id"] for p in prospects(owner, needs_verification=False)] == ["P-002"]
    assert [p["external_id"] for p in prospects(owner, freshness="stale")] == ["P-004"]

    # Verifying records who checked and when, and refreshes the check date.
    target = by_id(owner)["P-004"]
    detail = ok(owner.post(f"{API}/observations/{target['observation_id']}/verify", json={"state": "verified"}))
    assert detail["needs_verification"] == [] and detail["finding_state"] == "verified" and detail["is_stale"] is False
    assert detail["verification_state"] == "verified"
    top = by_id(owner)["P-001"]
    assert (
        owner.post(f"{API}/observations/{top['observation_id']}/verify", json={"state": "contradicted"}).status_code
        == 422
    )
    contradicted = ok(
        owner.post(
            f"{API}/observations/{top['observation_id']}/verify",
            json={"state": "contradicted", "note": "The site now has online booking"},
        )
    )
    assert (
        contradicted["needs_verification"] == ["finding_contradicted"]
        and contradicted["verification_state"] == "contradicted"
    )
    resolved = ok(
        owner.post(f"{API}/hypotheses/{top['hypothesis_id']}/resolve", json={"status": "rejected", "note": "Not true"})
    )
    assert resolved["hypothesis_status"] == "rejected" and resolved["finding"] != resolved["hypothesis"]

    # A shorter freshness window makes more evidence stale.
    assert (
        owner.put(f"{API}/queue-settings", json={"shortlist_size": 25, "evidence_freshness_days": 0}).status_code == 422
    )
    set_today(migrator_engine, owner, 3, ["P-002"])
    ok(owner.put(f"{API}/queue-settings", json={"shortlist_size": 25, "evidence_freshness_days": 2}))
    assert by_id(owner)["P-002"]["needs_verification"] == ["stale_evidence"]


def test_reviewed_edits_keep_history_and_survive_a_reimport(owner: TestClient) -> None:
    rows = [row("P-001", "Alpha", 85)]
    import_and_commit(owner, workbook_bytes(rows))
    lead = by_id(owner)["P-001"]
    edited = ok(
        owner.patch(
            f"{API}/prospects/{lead['lead_id']}/assessment",
            json={
                "outreach_opening": "Здравейте, видях че нямате онлайн резервации.",
                "channel_instruction": "Call after 14:00",
            },
        )
    )
    assert edited["outreach_opening"].startswith("Здравейте, видях") and edited["user_edited_fields"] == [
        "channel_instruction",
        "outreach_opening",
    ]
    assert (
        owner.patch(f"{API}/prospects/{lead['lead_id']}/assessment", json={"finding": "rewritten"}).status_code == 422
    )
    history = ok(owner.get(f"{API}/activities", params={"lead_id": lead["lead_id"], "kind": "prospect.edited"}))[
        "items"
    ]
    change = history[0]["data"]["changes"]["outreach_opening"]
    assert change["from"] == "Здравейте, разгледах сайта Ви." and change["to"].startswith("Здравейте, видях")

    rows[0]["Specific observed issue or opportunity"] = "New finding from a later check."
    rows[0]["Personalized outreach opening"] = "A different opening from the file"
    import_and_commit(owner, workbook_bytes(rows))
    after = by_id(owner)["P-001"]
    assert after["finding"] == "New finding from a later check."  # research refreshed
    assert after["outreach_opening"].startswith("Здравейте, видях")  # the reviewer's wording kept
    assert after["channel_instruction"] == "Call after 14:00"


def test_dismissal_needs_a_reason_and_can_be_undone(owner: TestClient, rep: TestClient) -> None:
    import_and_commit(owner, workbook_bytes([row("P-001", "Alpha", 85), row("P-002", "Bravo", 70)]))
    lead = by_id(owner)["P-001"]["lead_id"]
    assert rep.post(f"{API}/prospects/{lead}/dismiss", json={"reason": ""}).status_code == 422
    dismissed = ok(rep.post(f"{API}/prospects/{lead}/dismiss", json={"reason": "Closed permanently"}))
    assert dismissed["status"] == "disqualified" and dismissed["actions"]["call"] == {
        "available": False,
        "reason": "prospect_inactive",
    }
    assert all(c["dial_uri"] is None for c in dismissed["channels"])
    assert [p["external_id"] for p in prospects(owner)] == ["P-002"]  # hidden from the working list
    assert [p["external_id"] for p in prospects(owner, status="disqualified")] == ["P-001"]
    assert ok(owner.get(f"{API}/call-queue"))["entries"][0]["prospect"]["external_id"] == "P-002"
    restored = ok(rep.post(f"{API}/prospects/{lead}/restore"))
    assert restored["status"] == "qualified" and rep.post(f"{API}/prospects/{lead}/restore").status_code == 409


# --- the daily queue -------------------------------------------------------------------------


def test_call_queue_puts_due_follow_ups_first_then_phone_first_prospects(owner: TestClient) -> None:
    import_and_commit(
        owner,
        workbook_bytes(
            [
                row("P-001", "Email-first high score", 95, **{"Recommended contact channel": "Email"}),
                row("P-002", "Phone-first", 82),
                row("P-003", "Phone-first lower", 65),
                row("P-004", "Follow-up due", 61),
                row("P-005", "No phone", 99, **{"Public business phone": "Not found"}),
                row("P-006", "Only an emergency line", 97, **{"Public business phone": "(emergency: 0884 000 004)"}),
            ]
        ),
    )
    lead = by_id(owner)["P-004"]
    detail = ok(owner.get(f"{API}/prospects/{lead['lead_id']}"))
    phone = next(c for c in detail["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{lead['lead_id']}/calls", json={"channel_id": phone["id"]}), 201)
    due = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    ok(
        owner.post(
            f"{API}/calls/{call['id']}/outcome",
            json={"outcome": "follow_up_requested", "follow_up_at": due, "follow_up_note": "Call the manager back"},
        )
    )

    queue = ok(owner.get(f"{API}/call-queue"))
    order = [(e["prospect"]["external_id"], e["reason"], e["is_filler"]) for e in queue["entries"]]
    assert order == [
        ("P-004", "follow_up_due", True),
        ("P-002", "phone_first", False),
        ("P-003", "phone_first", True),  # Tier B shown as a filler, never as Tier A
        ("P-001", "callable", False),
    ]
    # P-005 has no phone and P-006 only an emergency line: neither is offered for calling.
    assert queue["requested_size"] == 25 and queue["shortfall"] == 21
    assert (
        queue["shortfall_reason"]
        == "Only 4 prospects with a number that may be called are available. The list is not padded."
    )
    assert "score (high to low)" in queue["tie_break"] and queue["timezone"] == "Europe/Sofia"
    blocked = by_id(owner)
    assert blocked["P-005"]["actions"]["call"] == {"available": False, "reason": "no_phone_found"}
    assert blocked["P-006"]["actions"]["call"] == {"available": False, "reason": "no_usable_number"}
    assert blocked["P-001"]["actions"]["email"] == {"available": False, "reason": "mailbox_not_connected"}

    # The score-ranked view is kept separately and ignores callability.
    ranked = ok(owner.post(f"{API}/shortlists", json={"kind": "score_ranked"}), 201)
    assert [e["prospect"]["external_id"] for e in ranked["entries"]] == [
        "P-005",
        "P-006",
        "P-001",
        "P-002",
        "P-003",
        "P-004",
    ]
    ok(owner.put(f"{API}/queue-settings", json={"shortlist_size": 2, "evidence_freshness_days": 30}))
    small = ok(owner.get(f"{API}/call-queue"))
    assert [e["prospect"]["external_id"] for e in small["entries"]] == ["P-004", "P-002"] and small["shortfall"] == 0


def test_shortlist_snapshots_are_dated_in_the_tenant_time_zone_and_keep_their_scores(owner: TestClient) -> None:
    import_and_commit(owner, workbook_bytes([row("P-001", "Alpha", 85), row("P-002", "Bravo", 70)]))
    ok(owner.patch(f"{API}/tenant", json={"timezone": "Pacific/Kiritimati"}))  # UTC+14
    ahead = ok(owner.post(f"{API}/shortlists", json={"kind": "call_queue"}), 201)
    ok(owner.patch(f"{API}/tenant", json={"timezone": "Pacific/Pago_Pago"}))  # UTC-11
    behind = ok(owner.post(f"{API}/shortlists", json={"kind": "call_queue"}), 201)
    now = datetime.now(UTC)
    assert ahead["shortlist_date"] == (now + timedelta(hours=14)).date().isoformat()
    assert behind["shortlist_date"] == (now - timedelta(hours=11)).date().isoformat()
    assert ahead["shortlist_date"] != behind["shortlist_date"]
    tomorrow = ok(owner.post(f"{API}/shortlists", json={"kind": "call_queue", "day": "tomorrow"}), 201)
    assert tomorrow["shortlist_date"] == (now - timedelta(hours=11) + timedelta(days=1)).date().isoformat()
    assert "tomorrow" not in tomorrow["name"].lower()

    # Generating again returns the same snapshot unless a new one is asked for.
    again = ok(owner.post(f"{API}/shortlists", json={"kind": "call_queue"}), 201)
    assert again["id"] == behind["id"]
    lead = behind["entries"][0]["prospect"]["lead_id"]
    ok(
        owner.post(
            f"{API}/leads/{lead}/scores",
            json={
                "components": {"evidence": 5, "relevance": 5, "value": 5, "reachability": 5, "activity": 5},
                "reason": "Site rebuilt",
            },
        ),
        201,
    )
    frozen = ok(owner.get(f"{API}/shortlists/{behind['id']}"))
    first = frozen["entries"][0]
    assert (first["score_at_snapshot"], first["tier_at_snapshot"]) == (85, "A")  # as ranked on the day
    assert (first["prospect"]["total"], first["prospect"]["tier"]) == (25, "C")  # as it is now
    fresh = ok(owner.post(f"{API}/shortlists", json={"kind": "call_queue", "regenerate": True}), 201)
    assert fresh["id"] != behind["id"] and fresh["entries"][0]["prospect"]["external_id"] == "P-002"
    listed = ok(owner.get(f"{API}/shortlists"))
    # "Tomorrow" in one zone can be the same calendar date as "today" in another, in which case it is reused.
    assert listed["total"] in (3, 4) and all(s["origin"] == "generated" for s in listed["items"])
    assert ok(owner.get(f"{API}/leads"))["total"] == 2  # a shortlist is a view, not more leads


# --- calls -----------------------------------------------------------------------------------


def test_opening_the_dialler_is_not_a_call_until_the_user_reports_an_outcome(
    rep: TestClient, owner: TestClient
) -> None:
    import_and_commit(owner, workbook_bytes([row("P-001", "Alpha", 85, **{"Public business phone": "0888 123 456"})]))
    lead = by_id(rep)["P-001"]["lead_id"]
    phone = next(c for c in ok(rep.get(f"{API}/prospects/{lead}"))["channels"] if c["kind"] == "phone")
    started = ok(rep.post(f"{API}/prospects/{lead}/calls", json={"channel_id": phone["id"]}), 201)
    assert started["dial_uri"] == "tel:+359888123456" and started["launched_at"] is not None
    assert started["outcome"] is None and started["outcome_reported_at"] is None
    detail = ok(rep.get(f"{API}/prospects/{lead}"))
    assert detail["outreach_status"] == "not_contacted"  # nothing is claimed yet
    assert [a["kind"] for a in ok(rep.get(f"{API}/activities", params={"lead_id": lead}))["items"]].count(
        "call.reported"
    ) == 0

    assert rep.post(f"{API}/calls/{started['id']}/outcome", json={"outcome": "follow_up_requested"}).status_code == 422
    assert rep.post(f"{API}/calls/{started['id']}/outcome", json={"outcome": "answered"}).status_code == 422
    assert (
        rep.post(f"{API}/calls/{started['id']}/outcome", json={"outcome": "connected", "duration": 120}).status_code
        == 422
    )
    reported = ok(
        rep.post(f"{API}/calls/{started['id']}/outcome", json={"outcome": "connected", "notes": "Spoke with the owner"})
    )
    assert reported["outcome"] == "connected" and reported["outcome_source"] == "user_reported"
    assert rep.post(f"{API}/calls/{started['id']}/outcome", json={"outcome": "no_answer"}).status_code == 409
    detail = ok(rep.get(f"{API}/prospects/{lead}"))
    assert detail["outreach_status"] == "contacted" and len(detail["calls"]) == 1
    activity = ok(rep.get(f"{API}/activities", params={"lead_id": lead, "kind": "call.reported"}))["items"][0]
    assert activity["data"] == {
        "outcome": "connected",
        "dialler_opened": True,
        "follow_up_at": None,
        "source": "user_reported",
    }

    # A call made from a desk phone is logged without a dialler launch.
    manual = ok(
        rep.post(f"{API}/prospects/{lead}/calls/manual", json={"outcome": "no_answer", "channel_id": phone["id"]}), 201
    )
    assert manual["launched_at"] is None and manual["outcome"] == "no_answer"
    assert ok(rep.get(f"{API}/prospects/{lead}"))["outreach_status"] == "attempted"


def test_follow_up_request_schedules_a_callback_and_records_what_was_asked(rep: TestClient, owner: TestClient) -> None:
    import_and_commit(owner, workbook_bytes([row("P-001", "Alpha", 85, **{"Public business email": "Not found"})]))
    lead = by_id(rep)["P-001"]["lead_id"]
    phone = next(c for c in ok(rep.get(f"{API}/prospects/{lead}"))["channels"] if c["kind"] == "phone")
    call = ok(rep.post(f"{API}/prospects/{lead}/calls", json={"channel_id": phone["id"]}), 201)
    when = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    bad_email = rep.post(
        f"{API}/calls/{call['id']}/outcome",
        json={"outcome": "follow_up_requested", "follow_up_at": when.isoformat(), "requested_email": "nope"},
    )
    assert bad_email.status_code == 422
    reported = ok(
        rep.post(
            f"{API}/calls/{call['id']}/outcome",
            json={
                "outcome": "follow_up_requested",
                "follow_up_at": when.isoformat(),
                "follow_up_note": "Send booking demo details",
                "follow_up_scope": "Details of the online booking demo",
                "requested_email": "Manager@Alpha.example.bg",
            },
        )
    )
    assert (
        reported["follow_up_scope"] == "Details of the online booking demo"
        and reported["requested_email"] == "manager@alpha.example.bg"
    )

    detail = ok(rep.get(f"{API}/prospects/{lead}"))
    assert detail["outreach_status"] == "follow_up" and detail["open_follow_ups"] == 1
    assert datetime.fromisoformat(detail["next_follow_up_at"]) == when
    task = ok(rep.get(f"{API}/tasks", params={"lead_id": lead}))["items"][0]
    assert (task["kind"], task["title"], task["description"]) == (
        "call",
        "Send booking demo details",
        "Details of the online booking demo",
    )
    assert task["assignee_user_id"] == ok(rep.get(f"{API}/auth/me"))["user"]["id"]
    email = next(c for c in detail["channels"] if c["kind"] == "email")
    assert email["label"] == "Given by phone for a requested follow-up" and email["source_type"] == "manual_entry"
    assert email["verification_state"] == "unverified"
    # The email action stays unavailable: giving an address for one follow-up connects no mailbox and grants no campaign consent.
    assert detail["actions"]["email"] == {"available": False, "reason": "mailbox_not_connected"}
    # Follow-ups are stored in the database and listed regardless of any email connection.
    assert ok(rep.get(f"{API}/prospects", params={"sort": "follow_up"}))["items"][0]["external_id"] == "P-001"
    ok(rep.patch(f"{API}/tasks/{task['id']}", json={"status": "done"}))
    assert ok(rep.get(f"{API}/prospects/{lead}"))["open_follow_ups"] == 0


def test_wrong_number_not_interested_and_do_not_call(rep: TestClient, owner: TestClient) -> None:
    import_and_commit(
        owner,
        workbook_bytes(
            [
                row("P-001", "Alpha", 85, **{"Public business phone": "0888 111 111; 0888 222 222"}),
                row("P-002", "Bravo", 80, **{"Public business phone": "0888 333 333"}),
            ]
        ),
    )
    alpha, bravo = by_id(rep)["P-001"]["lead_id"], by_id(rep)["P-002"]["lead_id"]
    phones = [c for c in ok(rep.get(f"{API}/prospects/{alpha}"))["channels"] if c["kind"] == "phone"]
    call = ok(rep.post(f"{API}/prospects/{alpha}/calls", json={"channel_id": phones[0]["id"]}), 201)
    ok(rep.post(f"{API}/calls/{call['id']}/outcome", json={"outcome": "wrong_number"}))
    detail = ok(rep.get(f"{API}/prospects/{alpha}"))
    wrong = next(c for c in detail["channels"] if c["id"] == phones[0]["id"])
    assert wrong["verification_state"] == "invalid" and wrong["dial_uri"] is None
    assert detail["dialable_count"] == 1 and detail["actions"]["call"]["available"]
    assert rep.post(f"{API}/prospects/{alpha}/calls", json={"channel_id": phones[0]["id"]}).status_code == 409

    bravo_phone = next(c for c in ok(rep.get(f"{API}/prospects/{bravo}"))["channels"] if c["kind"] == "phone")
    assert rep.post(f"{API}/prospects/{alpha}/calls", json={"channel_id": bravo_phone["id"]}).status_code == 404
    call = ok(rep.post(f"{API}/prospects/{bravo}/calls", json={"channel_id": bravo_phone["id"]}), 201)
    assert (
        rep.post(
            f"{API}/calls/{call['id']}/outcome", json={"outcome": "not_interested", "do_not_call": True}
        ).status_code
        == 422
    )
    ok(
        rep.post(
            f"{API}/calls/{call['id']}/outcome",
            json={"outcome": "not_interested", "do_not_call": True, "do_not_call_reason": "Asked us not to call again"},
        )
    )
    after = ok(rep.get(f"{API}/prospects/{bravo}"))
    assert after["outreach_status"] == "not_interested" and after["restriction_reasons"] == "Asked us not to call again"
    assert after["actions"]["call"] == {"available": False, "reason": "prospect_inactive"}
    assert all(c["dial_uri"] is None for c in after["channels"])
    assert rep.post(f"{API}/prospects/{bravo}/calls", json={"channel_id": bravo_phone["id"]}).status_code == 409
    assert [e["prospect"]["external_id"] for e in ok(rep.get(f"{API}/call-queue"))["entries"]] == ["P-001"]
    assert "restriction.created" in [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]


def test_emergency_and_restricted_numbers_cannot_be_dialled(rep: TestClient, owner: TestClient) -> None:
    import_and_commit(
        owner,
        workbook_bytes(
            [row("P-001", "Vet", 85, **{"Public business phone": "0887 000 003 (emergency: 0884 000 004)"})]
        ),
    )
    lead = by_id(rep)["P-001"]["lead_id"]
    channels = {c["purpose"]: c for c in ok(rep.get(f"{API}/prospects/{lead}"))["channels"] if c["kind"] == "phone"}
    refused = rep.post(f"{API}/prospects/{lead}/calls", json={"channel_id": channels["emergency"]["id"]})
    assert refused.status_code == 409 and "emergency line" in refused.json()["error"]["message"]
    company = by_id(rep)["P-001"]["company_id"]
    ok(
        owner.post(
            f"{API}/companies/{company}/restrictions", json={"channel_kind": "phone", "reason": "Owner asked: no calls"}
        ),
        201,
    )
    blocked = rep.post(f"{API}/prospects/{lead}/calls", json={"channel_id": channels["general"]["id"]})
    assert blocked.status_code == 409 and "do not call" in blocked.json()["error"]["message"]
    assert by_id(rep)["P-001"]["actions"]["call"] == {"available": False, "reason": "do_not_call"}
    assert ok(rep.get(f"{API}/call-queue"))["entries"] == []


def test_workspace_permissions_and_isolation(owner: TestClient, make_client: Any) -> None:
    import_and_commit(owner, workbook_bytes([row("P-001", "Alpha", 85)]))
    lead = by_id(owner)["P-001"]
    phone = next(c for c in ok(owner.get(f"{API}/prospects/{lead['lead_id']}"))["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{lead['lead_id']}/calls", json={"channel_id": phone["id"]}), 201)
    snapshot = ok(owner.post(f"{API}/shortlists", json={}), 201)

    viewer = make_client()
    sign_in(viewer, "viewer@example.test")
    join(owner, viewer, "viewer@example.test", "read_only")
    assert ok(viewer.get(f"{API}/prospects"))["total"] == 1
    seen = ok(viewer.get(f"{API}/prospects/{lead['lead_id']}"))
    assert seen["actions"]["call"] == {"available": False, "reason": "no_permission"} and all(
        c["dial_uri"] is None for c in seen["channels"]
    )
    for method, path, body in (
        ("POST", f"/prospects/{lead['lead_id']}/calls", {"channel_id": phone["id"]}),
        ("POST", f"/calls/{call['id']}/outcome", {"outcome": "connected"}),
        ("POST", f"/prospects/{lead['lead_id']}/calls/manual", {"outcome": "connected", "dialed_value": "1"}),
        ("PATCH", f"/prospects/{lead['lead_id']}/assessment", {"notes": "x"}),
        ("POST", f"/observations/{lead['observation_id']}/verify", {"state": "verified"}),
        ("POST", f"/prospects/{lead['lead_id']}/dismiss", {"reason": "nope"}),
        ("POST", "/shortlists", {}),
        ("PUT", "/queue-settings", {"shortlist_size": 5, "evidence_freshness_days": 5}),
    ):
        assert viewer.request(method, f"{API}{path}", json=body).status_code == 403, path

    other = make_client()
    sign_in(other, "other@example.test")
    create_workspace(other, "SEWEB")
    assert ok(other.get(f"{API}/prospects"))["total"] == 0 and ok(other.get(f"{API}/call-queue"))["entries"] == []
    for method, path, body in (
        ("GET", f"/prospects/{lead['lead_id']}", None),
        ("GET", f"/shortlists/{snapshot['id']}", None),
        ("POST", f"/prospects/{lead['lead_id']}/calls", {"channel_id": phone["id"]}),
        ("POST", f"/calls/{call['id']}/outcome", {"outcome": "connected"}),
        ("POST", f"/observations/{lead['observation_id']}/verify", {"state": "verified"}),
        ("POST", f"/hypotheses/{lead['hypothesis_id']}/resolve", {"status": "confirmed"}),
        ("POST", f"/prospects/{lead['lead_id']}/dismiss", {"reason": "nope"}),
    ):
        assert other.request(method, f"{API}{path}", json=body).status_code == 404, path
    assert ok(owner.get(f"{API}/prospects/{lead['lead_id']}"))["calls"][0]["outcome"] is None


@pytest.mark.reference_fixture
def test_reference_call_queue_differs_from_the_score_ranked_shortlist(
    owner: TestClient, reference_workbook: Any, migrator_engine: Any
) -> None:
    import_and_commit(owner, reference_workbook.read_bytes(), "reference.xlsx")
    set_today(migrator_engine, owner, 1)
    everyone = ok(owner.get(f"{API}/prospects", params={"limit": 200}))
    assert everyone["total"] == 94
    # Every Tier A lead waits for verification: imported research is not verified research.
    assert sum(1 for p in everyone["items"] if p["needs_verification"] == ["unverified_high_priority"]) == 21
    assert sum(1 for p in everyone["items"] if "low_confidence" in p["needs_verification"]) == 5
    assert sum(1 for p in everyone["items"] if p["actions"]["call"]["available"]) == 93
    assert sum(1 for p in everyone["items"] if p["actions"]["call"]["reason"] == "no_phone_found") == 1

    ranked = ok(owner.post(f"{API}/shortlists", json={"kind": "score_ranked"}), 201)
    assert len(ranked["entries"]) == 25 and ranked["filler_count"] == 4
    assert min(e["score_at_snapshot"] for e in ranked["entries"]) == 77
    imported = next(s for s in ok(owner.get(f"{API}/shortlists"))["items"] if s["origin"] == "import")
    source = ok(owner.get(f"{API}/shortlists/{imported['id']}"))
    assert {e["prospect"]["external_id"] for e in ranked["entries"]} == {
        e["prospect"]["external_id"] for e in source["entries"]
    }

    queue = ok(owner.get(f"{API}/call-queue"))
    assert len(queue["entries"]) == 25 and queue["shortfall"] == 0
    assert all(e["reason"] == "phone_first" and e["prospect"]["dialable_count"] > 0 for e in queue["entries"])
    # Call-first ordering intentionally differs from the score-ranked list: email-first leads drop out.
    assert {e["prospect"]["external_id"] for e in queue["entries"]} != {
        e["prospect"]["external_id"] for e in ranked["entries"]
    }
    assert ok(owner.get(f"{API}/leads"))["total"] == 94
