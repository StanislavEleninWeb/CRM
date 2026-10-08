"""Research run through the real scheduler and worker, with the local test adapters.

Run:  docker compose exec api python /infra/e2e/phase_b_research.py   (on a fresh stack)

No real provider is contacted and nothing is paid for. The local test discovery adapter
returns made-up listings whose websites do not exist, so every candidate ends in review
with the reason recorded. What this proves is the plumbing: a run is a due row, the
scheduler claims it, the worker advances it, costs are reserved and settled, and nothing
becomes a lead without a person.
"""

import sys

sys.path.insert(0, "/infra/e2e")
sys.path.insert(0, "/app")
from client import ok, sign_in, wait_for  # noqa: E402

checks = 0


def check(condition: bool, label: str) -> None:
    global checks  # noqa: PLW0603
    if not condition:
        raise SystemExit(f"FAIL: {label}")
    checks += 1
    print(f"  ok  {label}")


owner = sign_in("owner@example.test")
if ok(owner.get("/api/v1/auth/me"))["active_tenant"] is None:
    ok(owner.post("/api/v1/tenants", json={"name": "Research check"}), 201)
adapters = {a["name"]: a for a in ok(owner.get("/api/v1/provider-adapters"))}
check(adapters["fake_discovery"]["is_local_test_adapter"] and adapters["fake_model"]["is_local_test_adapter"], "only local test adapters are used")
discovery = ok(owner.post("/api/v1/provider-connections", json={"provider": "fake_discovery", "label": "Local discovery", "credential": "local-test-key-1"}), 201)
model = ok(owner.post("/api/v1/provider-connections", json={"provider": "fake_model", "label": "Local model", "credential": "local-test-key-2"}), 201)
config_body = {"name": "Local plumbing check", "country": "Bulgaria", "cities": ["Sofia"], "categories": ["hair salon", "vet clinic"],
               "services": ["Online booking"], "cost_cap": "1", "candidate_cap": 20, "qualified_target": 5,
               "discovery_connection_id": discovery["id"], "model_connection_id": model["id"]}
config = ok(owner.post("/api/v1/research-configs", json=config_body), 201)

refused = owner.post(f"/api/v1/research-configs/{config['id']}/runs", json={})
check(refused.status_code == 201, "a run can be queued before a budget exists")
blocked = wait_for(owner, f"/api/v1/research-runs/{refused.json()['id']}", {"paused", "completed", "failed"})
check(blocked["status"] == "paused" and "budget" in (blocked["error"] or "").lower(), "without a budget the run pauses instead of spending")

ok(owner.put("/api/v1/budgets", json={"scope": "research", "limit_amount": "2"}))
ok(owner.post(f"/api/v1/research-runs/{blocked['id']}/resume"))
done = wait_for(owner, f"/api/v1/research-runs/{blocked['id']}", {"completed", "failed", "paused"}, timeout=150)
check(done["status"] == "completed", "the scheduler and worker advanced the run to completion")
check((done["searches_done"], done["searches_total"]) == (2, 2), "both searches in the matrix were executed")
summary = done["summary"]
check(summary["qualified"] == 0 and summary["shortfall"] == 5 and "not padded" in summary["shortfall_note"], "no candidate qualified and the result was not padded")
candidates = ok(owner.get("/api/v1/research-candidates", params={"run_id": done["id"], "limit": 100}))["items"]
check(len(candidates) == 6 and all(c["state"] == "needs_review" for c in candidates), "all six candidates wait for human review")
check(all("could not be read automatically" in c["state_reason"] for c in candidates), "the reason is an access limitation, not a claim about the business")
check(all(c["name"] is None and c["website_url"] is None and c["listing_id_type"] == "place_id" for c in candidates), "only the place ID was stored from the provider")
check(all("name" in c["dropped_fields"] and "website_url" in c["dropped_fields"] for c in candidates), "dropped provider fields are named")
usage = ok(owner.get("/api/v1/usage/summary"))
check(usage["estimated_amount"] == "0.0200" and usage["reserved_amount"] == "0.0000" and usage["verified_amount"] == "0.0000",
      "two searches were settled as estimates; nothing is left reserved")
check(ok(owner.get("/api/v1/leads"))["total"] == 0, "nothing became a lead without a person")
print(f"\nResearch plumbing: {checks} checks passed (local test adapters; no real provider contacted)")
