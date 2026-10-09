"""Calling the SEWEB CRM API with a dedicated, least-privilege key.

Standard library only, so it runs anywhere Python does. Configuration comes from the
environment; a key is never written into a file or a prompt:

    export CRM_BASE_URL=https://crm.example.com      # or http://localhost:8000 for local development
    export CRM_API_KEY=crm_...                       # created under Integrations -> API keys

Run the read-only tour:   python crm_api.py

Things worth knowing:
  * The key acts inside one workspace. There is no way to name another one.
  * A key cannot approve an email, change settings or members, or read stored credentials.
  * Send ``Idempotency-Key`` on anything that creates or changes data. Repeating the exact
    request returns the first answer; reusing the key for a different request is an error.
    After a 5xx the same key answers 409: the work may have been done, so read the state first.
  * 429 means slow down; wait for the number of seconds in ``Retry-After``.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any
from urllib import error, parse, request


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def call(method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
         idempotency_key: str | None = None) -> Any:
    base = os.environ["CRM_BASE_URL"].rstrip("/")
    url = f"{base}/api/v1{path}"
    if params:
        url += "?" + parse.urlencode({k: v for k, v in params.items() if v is not None})
    headers = {"Authorization": f"Bearer {os.environ['CRM_API_KEY']}", "Accept": "application/json"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 - the base URL is the operator's own
    try:
        with request.urlopen(req, timeout=30) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None
    except error.HTTPError as exc:
        try:
            detail = json.loads(exc.read())["error"]
        except (ValueError, KeyError):
            detail = {"code": "error", "message": exc.reason}
        raise ApiError(exc.code, detail.get("code", "error"), detail.get("message", "")) from exc


# 1. Search prospects.
def search_prospects(text: str | None = None, *, city: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    return call("GET", "/prospects", params={"q": text, "city": city, "limit": limit})["items"]  # type: ignore[no-any-return]


# 2. Start a research run. Its limits (cost cap, candidate cap, target) belong to a
#    configuration a person created; a key cannot raise them. Needs the research.run scope.
def start_research(config_id: str, *, request_id: str | None = None) -> dict[str, Any]:
    key = request_id or f"research-{config_id}-{time.strftime('%Y-%m-%d')}"  # at most one run per configuration per day
    return call("POST", f"/research-configs/{config_id}/runs", body={}, idempotency_key=key)  # type: ignore[no-any-return]


# 3. Read the evidence behind a prospect: scores, observations with their sources, contact channels.
def get_evidence(lead_id: str) -> dict[str, Any]:
    return call("GET", f"/prospects/{lead_id}")  # type: ignore[no-any-return]


# 4. Create an email draft. It is only a draft: a person approves it, and the answer says
#    whether the outreach rules would allow it and why not. Needs the outreach.draft scope.
def create_draft(lead_id: str, *, subject: str | None = None, body_text: str | None = None,
                 request_id: str | None = None) -> dict[str, Any]:
    payload = {"lead_id": lead_id, "kind": "unsolicited", "subject": subject, "body_text": body_text}
    return call("POST", "/email-drafts", body=payload, idempotency_key=request_id or str(uuid.uuid4()))  # type: ignore[no-any-return]


# 5. Update a task. Needs the crm.write scope.
def update_task(task_id: str, **changes: Any) -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()[:16]
    return call("PATCH", f"/tasks/{task_id}", body=changes, idempotency_key=f"task-{task_id}-{digest}")  # type: ignore[no-any-return]


# 6. Read today's call shortlist.
def daily_shortlist() -> dict[str, Any]:
    return call("GET", "/call-queue")  # type: ignore[no-any-return]


def verify_webhook(secret: str, signature_header: str, raw_body: bytes, *, tolerance_seconds: int = 300) -> bool:
    """Check a webhook before trusting it. ``raw_body`` is the bytes as received, before any JSON parsing.

    Deliveries are at least once: remember the event ``id`` and ignore one you have already handled.
    """
    parts = [p.split("=", 1) for p in signature_header.split(",") if "=" in p]
    stamps = [v for k, v in parts if k == "t"]
    if len(stamps) != 1 or not stamps[0].isdigit() or abs(time.time() - int(stamps[0])) > tolerance_seconds:
        return False
    expected = hmac.new(secret.encode(), f"{stamps[0]}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in parts if k == "v1")


if __name__ == "__main__":
    queue = daily_shortlist()
    print(f"Today's shortlist ({queue['queue_date']}): {len(queue['entries'])} prospects")
    for prospect in search_prospects(limit=5):
        print(f"- {prospect['company_name']} ({prospect.get('city') or 'no city'})")
