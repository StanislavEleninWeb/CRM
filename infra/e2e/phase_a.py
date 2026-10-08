"""Phase A handoff check against a running local stack, using the real background worker.

Run:  docker compose exec api python /infra/e2e/phase_a.py

Covers: two-tenant isolation, import -> score -> queue -> call outcome -> follow-up,
date-only stability, and that nothing depends on a mailbox, an AI key or billing.
Uses the private reference workbook when it is present; otherwise a synthetic list.
Start from an empty database (``make reset && make up``).
"""

import io
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/infra/e2e")
sys.path.insert(0, "/app")

from client import ok, sign_in, wait_for  # noqa: E402

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REFERENCE = Path("/fixtures/private/SEWEB_prospects_Bulgaria_2026-10-07.xlsx")
checks: list[str] = []


def check(condition: bool, label: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {label}")
    checks.append(label)
    print(f"  ok  {label}")


def synthetic_workbook() -> bytes:
    from tests.test_import import workbook_bytes
    from tests.test_prospects import row

    rows = [row(f"E-{i:03d}", f"Synthetic Company {i}", 95 - i * 3) for i in range(1, 13)]
    return workbook_bytes(rows, shortlist=[r["Lead ID"] for r in rows[:5]])


def import_file(client, data: bytes) -> dict:
    created = ok(client.post("/api/v1/imports", files={"file": ("prospects.xlsx", io.BytesIO(data), XLSX)}), 202)
    ready = wait_for(client, f"/api/v1/imports/{created['id']}", {"ready", "failed"})
    check(ready["status"] == "ready", "worker parsed the upload into a preview")
    check(ok(client.get("/api/v1/leads"))["total"] == 0 or ready["report"]["counts"].get("update"), "preview created no leads")
    ok(client.post(f"/api/v1/imports/{created['id']}/commit"), 202)
    done = wait_for(client, f"/api/v1/imports/{created['id']}", {"committed", "failed"})
    check(done["status"] == "committed", "worker committed the import")
    return done


def main() -> None:
    using_reference = REFERENCE.exists()
    data = REFERENCE.read_bytes() if using_reference else synthetic_workbook()
    print(f"Fixture: {'private reference workbook' if using_reference else 'synthetic list (reference gate NOT exercised)'}")

    print("Tenant A: owner signs in and creates the workspace")
    owner = sign_in("owner@example.test")
    tenant_a = ok(owner.post("/api/v1/tenants", json={"name": "SEWEB"}), 201)
    check(tenant_a["currency"] == "EUR" and tenant_a["timezone"] == "Europe/Sofia", "tenant defaults are EUR and Europe/Sofia")

    print("Import")
    done = import_file(owner, data)
    total = ok(owner.get("/api/v1/leads"))["total"]
    check(total == done["result"]["created"] and total > 0, f"{total} leads created")
    if using_reference:
        check(total == 94, "reference workbook produced exactly 94 leads")
        tiers = {t: ok(owner.get("/api/v1/prospects", params={"tier": t, "limit": 1}))["total"] for t in "ABC"}
        check(tiers == {"A": 21, "B": 62, "C": 11}, "tiers are 21 A / 62 B / 11 C")

    print("Score and queue")
    top = ok(owner.get("/api/v1/prospects", params={"limit": 5}))["items"]
    check(all(p["total"] == sum(p["components"].values()) for p in top), "totals equal the sum of their components")
    check(len({p["checked_on"] for p in top}) == 1 and len(top[0]["checked_on"]) == 10, "check date is a plain date")
    queue = ok(owner.get("/api/v1/call-queue"))
    check(len(queue["entries"]) > 0, f"call queue has {len(queue['entries'])} entries for {queue['queue_date']} ({queue['timezone']})")
    check(all(e["prospect"]["dialable_count"] > 0 for e in queue["entries"]), "every queued prospect has a number that may be called")
    check(all(e["is_filler"] == (e["prospect"]["tier"] != "A") for e in queue["entries"]), "entries below Tier A are labelled as fillers")
    check(all(e["prospect"]["actions"]["email"]["available"] is False for e in queue["entries"]), "email is unavailable (no mailbox) and blocks nothing")

    print("Representative joins and works the queue")
    invitation = ok(owner.post("/api/v1/invitations", json={"email": "rep@example.test", "role": "representative"}), 201)
    rep = sign_in("rep@example.test")
    ok(rep.post("/api/v1/invitations/accept", json={"token": invitation["token"]}))
    first = queue["entries"][0]["prospect"]
    detail = ok(rep.get(f"/api/v1/prospects/{first['lead_id']}"))
    phone = next(c for c in detail["channels"] if c["dial_uri"])
    started = ok(rep.post(f"/api/v1/prospects/{first['lead_id']}/calls", json={"channel_id": phone["id"]}), 201)
    check(started["dial_uri"].startswith("tel:+") and started["outcome"] is None, "dialler launch recorded without any outcome")
    check(ok(rep.get(f"/api/v1/prospects/{first['lead_id']}"))["outreach_status"] == "not_contacted", "opening the dialler did not mark the prospect contacted")
    when = datetime.now(UTC) - timedelta(minutes=1)
    ok(rep.post(f"/api/v1/calls/{started['id']}/outcome", json={
        "outcome": "follow_up_requested", "follow_up_at": when.isoformat(), "follow_up_note": "Call back about booking",
        "follow_up_scope": "Online booking demo"}))
    after = ok(rep.get(f"/api/v1/prospects/{first['lead_id']}"))
    check(after["outreach_status"] == "follow_up" and after["open_follow_ups"] == 1, "reported outcome scheduled a follow-up")
    queue = ok(rep.get("/api/v1/call-queue"))
    check(queue["entries"][0]["prospect"]["lead_id"] == first["lead_id"] and queue["entries"][0]["reason"] == "follow_up_due",
          "the due follow-up is first in the queue")
    tasks = ok(rep.get("/api/v1/tasks", params={"lead_id": first["lead_id"]}))["items"]
    check(len(tasks) == 1 and tasks[0]["kind"] == "call", "the follow-up is a stored task, independent of email")
    kinds = [a["kind"] for a in ok(rep.get("/api/v1/activities", params={"lead_id": first["lead_id"]}))["items"]]
    check("call.reported" in kinds and "lead.imported" in kinds, "history shows the import and the reported call")

    print("Verification and review")
    pending = ok(owner.get("/api/v1/prospects", params={"needs_verification": True, "limit": 1}))["total"]
    check(pending > 0, f"{pending} prospects wait for verification (imported research is unverified)")
    target = ok(owner.get("/api/v1/prospects", params={"needs_verification": True, "limit": 1}))["items"][0]
    verified = ok(owner.post(f"/api/v1/observations/{target['observation_id']}/verify", json={"state": "verified"}))
    check(verified["finding_state"] == "verified", "a reviewer verified a finding")
    snapshot = ok(owner.post("/api/v1/shortlists", json={"kind": "call_queue"}), 201)
    check(snapshot["shortlist_date"] == queue["queue_date"] and len(snapshot["entries"]) > 0, "today's queue saved as a dated snapshot")
    check(ok(owner.get("/api/v1/leads"))["total"] == total, "snapshots and queues created no extra leads")

    print("Tenant B: a separate workspace with the same name")
    other = sign_in("other@example.test")
    ok(other.post("/api/v1/tenants", json={"name": "SEWEB"}), 201)
    for path in ("/api/v1/leads", "/api/v1/companies", "/api/v1/prospects", "/api/v1/tasks", "/api/v1/shortlists"):
        check(ok(other.get(path))["total"] == 0, f"tenant B sees nothing at {path}")
    check(ok(other.get("/api/v1/call-queue"))["entries"] == [], "tenant B's call queue is empty")
    for path in (f"/api/v1/prospects/{first['lead_id']}", f"/api/v1/companies/{first['company_id']}", f"/api/v1/shortlists/{snapshot['id']}"):
        check(other.get(path).status_code == 404, f"tenant B gets 404 for tenant A's {path.split('/')[3]}")
    check(other.post(f"/api/v1/calls/{started['id']}/outcome", json={"outcome": "connected"}).status_code == 404, "tenant B cannot report on tenant A's call")
    check(other.get("/api/v1/exports/prospects.xlsx").status_code == 200, "tenant B can export its own (empty) list")
    check(rep.get("/api/v1/exports/prospects.xlsx").status_code == 403, "a representative cannot export")

    print("Re-import keeps the work done")
    again = import_file(owner, data)
    check(again["result"].get("created") is None, "re-import created no leads")
    check(ok(rep.get(f"/api/v1/prospects/{first['lead_id']}"))["outreach_status"] == "follow_up", "outreach status survived the re-import")

    print(f"\nPhase A handoff: {len(checks)} checks passed" + ("" if using_reference else " (synthetic data; reference gate not exercised)"))


if __name__ == "__main__":
    main()
