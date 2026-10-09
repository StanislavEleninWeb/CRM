"""The documented API examples, run against the local stack with synthetic records.

Run:  docker compose exec api python /infra/e2e/phase_c_api.py   (on a fresh stack)

It signs in as an administrator would, creates a least-privilege key, then calls
``examples/api/crm_api.py`` exactly as a customer's script would: over HTTP, with only the key.
No real provider, mailbox or webhook receiver is contacted.
"""

import io
import os
import sys
import uuid

sys.path.insert(0, "/infra/e2e")
sys.path.insert(0, "/app")
sys.path.insert(0, "/repo/examples/api")
from client import API_URL, ok, sign_in, wait_for  # noqa: E402
from phase_a import XLSX, synthetic_workbook  # noqa: E402

checks = 0


def check(condition: bool, label: str) -> None:
    global checks  # noqa: PLW0603
    if not condition:
        raise SystemExit(f"FAIL: {label}")
    checks += 1
    print(f"  ok  {label}")


owner = sign_in("owner@example.test")
if ok(owner.get("/api/v1/auth/me"))["active_tenant"] is None:
    ok(owner.post("/api/v1/tenants", json={"name": "API examples"}), 201)
created = ok(owner.post("/api/v1/imports", files={"file": ("prospects.xlsx", io.BytesIO(synthetic_workbook()), XLSX)}), 202)
wait_for(owner, f"/api/v1/imports/{created['id']}", {"ready"})
ok(owner.post(f"/api/v1/imports/{created['id']}/commit"), 202)
wait_for(owner, f"/api/v1/imports/{created['id']}", {"committed"})

scopes = ["crm.read", "crm.write", "research.run", "outreach.draft", "reports.read"]
key = ok(owner.post("/api/v1/api-keys", json={"name": "Examples check", "scopes": scopes}), 201)
check(key["key"].startswith("crm_") and "key" not in ok(owner.get("/api/v1/api-keys"))[0], "the key is shown once")
os.environ["CRM_BASE_URL"], os.environ["CRM_API_KEY"] = API_URL, key["key"]
import crm_api  # noqa: E402

prospects = crm_api.search_prospects(limit=5)
check(len(prospects) > 0 and all("company_name" in p for p in prospects), "example 1: search prospects")
queue = crm_api.daily_shortlist()
check(len(queue["entries"]) > 0, "example 6: read the daily shortlist")
lead_id = queue["entries"][0]["prospect"]["lead_id"]
evidence = crm_api.get_evidence(lead_id)
check(evidence["lead_id"] == lead_id and "channels" in evidence and "scores" in evidence, "example 3: get the evidence for a prospect")

task = ok(owner.post("/api/v1/tasks", json={"title": "Send the price list", "lead_id": lead_id}), 201)
done = crm_api.update_task(task["id"], status="done")
check(done["status"] == "done" and crm_api.update_task(task["id"], status="done")["status"] == "done", "example 5: update a task, repeatably")

try:
    draft = crm_api.create_draft(lead_id, subject="Online booking", body_text="Hello,", request_id="draft-check-1")
    again = crm_api.create_draft(lead_id, subject="Online booking", body_text="Hello,", request_id="draft-check-1")
    check(draft["id"] == again["id"] and draft["status"] == "draft", "example 4: create a draft once, however often it is asked")
    check(draft["eligibility"]["outcome"] == "block", "the draft says the outreach rules do not allow sending yet")
    drafted = True
except crm_api.ApiError as exc:
    check(exc.status == 422 and "No email address is recorded" in exc.message, "example 4: no address on file, so no draft and none invented")
    drafted = False
try:
    crm_api.call("POST", f"/email-drafts/{draft['id'] if drafted else uuid.uuid4()}/approve", body={})
    check(False, "a key cannot approve")
except crm_api.ApiError as exc:
    check(exc.status == 403, "a key cannot approve an email")

adapters = {a["name"] for a in ok(owner.get("/api/v1/provider-adapters"))}
if {"fake_discovery", "fake_model"} <= adapters:
    discovery = ok(owner.post("/api/v1/provider-connections", json={"provider": "fake_discovery", "label": "Local discovery", "credential": "local-test-key-1"}), 201)
    model = ok(owner.post("/api/v1/provider-connections", json={"provider": "fake_model", "label": "Local model", "credential": "local-test-key-2"}), 201)
    config = ok(owner.post("/api/v1/research-configs", json={
        "name": "API example", "country": "Bulgaria", "cities": ["Sofia"], "categories": ["hair salon"], "cost_cap": "1",
        "candidate_cap": 5, "qualified_target": 2, "discovery_connection_id": discovery["id"], "model_connection_id": model["id"]}), 201)
    run = crm_api.start_research(config["id"])
    check(run["status"] in ("queued", "running") and crm_api.start_research(config["id"])["id"] == run["id"],
          "example 2: start one bounded research run; asking twice starts one")
    try:
        crm_api.call("PATCH", f"/research-configs/{config['id']}", body={"cost_cap": "1000"})
        check(False, "a key cannot raise limits")
    except crm_api.ApiError as exc:
        check(exc.status in (403, 404, 405), "a key cannot raise the run's limits")

reader = ok(owner.post("/api/v1/api-keys", json={"name": "Read only", "scopes": ["crm.read"]}), 201)
os.environ["CRM_API_KEY"] = reader["key"]
try:
    crm_api.update_task(task["id"], status="open")
    check(False, "read-only key must not update")
except crm_api.ApiError as exc:
    check(exc.status == 403 and exc.code == "permission_denied", "a read-only key cannot update")
ok(owner.delete(f"/api/v1/api-keys/{reader['id']}"))
try:
    crm_api.daily_shortlist()
    check(False, "revoked key must fail")
except crm_api.ApiError as exc:
    check(exc.status == 401, "a revoked key stops working at once")

for address in ("http://localhost:9000/hook", "https://169.254.169.254/x", "https://10.0.0.8/hook"):
    refused = owner.post("/api/v1/webhook-endpoints", json={"url": address})
    check(refused.status_code == 422, f"webhook address refused: {address}")
audit = [e for e in ok(owner.get("/api/v1/audit-events"))["items"] if e["actor_type"] == "integration"]
check(all(e["data"].get("api_key_id") for e in audit), "what the key did is attributed to the key")
print(f"\nAPI examples: {checks} checks passed against the local stack with synthetic records")
