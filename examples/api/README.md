# API examples

Everything here uses a dedicated API key with only the scopes it needs. Create one under **Integrations → API keys**; it is shown once.

```bash
export CRM_BASE_URL=http://localhost:8000
export CRM_API_KEY=crm_...
```

`crm_api.py` has the same six calls as Python functions, plus `verify_webhook`.

## curl

Search prospects (scope `crm.read`):

```bash
curl -sS "$CRM_BASE_URL/api/v1/prospects?q=salon&limit=5" -H "Authorization: Bearer $CRM_API_KEY"
```

Read today's call shortlist (`crm.read`):

```bash
curl -sS "$CRM_BASE_URL/api/v1/call-queue" -H "Authorization: Bearer $CRM_API_KEY"
```

Read the evidence behind one prospect (`crm.read`):

```bash
curl -sS "$CRM_BASE_URL/api/v1/prospects/$LEAD_ID" -H "Authorization: Bearer $CRM_API_KEY"
```

Start a research run within the limits of an existing configuration (`research.run`):

```bash
curl -sS -X POST "$CRM_BASE_URL/api/v1/research-configs/$CONFIG_ID/runs" \
  -H "Authorization: Bearer $CRM_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: research-$CONFIG_ID-$(date +%F)" -d '{}'
```

Create an email draft (`outreach.draft`). A key can never approve or bypass the outreach rules:

```bash
curl -sS -X POST "$CRM_BASE_URL/api/v1/email-drafts" \
  -H "Authorization: Bearer $CRM_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" -d "{\"lead_id\": \"$LEAD_ID\", \"kind\": \"unsolicited\"}"
```

Mark a task done (`crm.write`):

```bash
curl -sS -X PATCH "$CRM_BASE_URL/api/v1/tasks/$TASK_ID" \
  -H "Authorization: Bearer $CRM_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: task-$TASK_ID-done" -d '{"status": "done"}'
```

## Behaviour to rely on

| Topic | Rule |
|---|---|
| Errors | Always `{"error": {"code", "message", "correlation_id"}}`. Quote the correlation ID when asking for help. |
| Paging | List endpoints take `limit` (max 200) and `offset`, and return `items`, `total`, `limit`, `offset`. |
| Idempotency | `Idempotency-Key` on POST, PUT, PATCH, DELETE made with a key. Same request: the first answer again, with `Idempotency-Replayed: true`. Different request: `422 idempotency_conflict`. Still running or outcome not recorded: `409 idempotency_in_progress` — read the current state; it is never run twice. |
| Rate limit | Per key, per minute. `429` with `Retry-After`. |
| Scopes | A key can hold: `crm.read`, `crm.write`, `research.review`, `research.run`, `outreach.draft`, `outreach.send`, `calls.log`, `reports.read`. Nothing else. It never exceeds what the person who created it may do. |
| Tenant | Fixed by the key. A tenant named in a header, query or body is ignored or rejected. |

## Webhooks

Register an HTTPS address under **Integrations → Webhooks**. Each delivery is a POST with:

- `X-CRM-Signature: t=<unix seconds>,v1=<hex>` — HMAC-SHA256 of `"<t>." + raw body` with the endpoint's secret. During the 24 hours after a secret rotation a second `v1` made with the old secret is included.
- `X-CRM-Event-Id` — stable for the event. **Delivery is at least once**: the same event can arrive again after a timeout or a manual replay. Store the IDs you have handled and skip repeats.
- Body: `{"id", "type", "api_version", "created_at", "tenant_id", "subject": {"type", "id"}, "data"}`.

Answer with any 2xx within 10 seconds. Anything else, including a redirect, is retried after 1 minute, 5 minutes, 30 minutes, 2 hours, 6 hours and 24 hours, then marked dead until someone replays it. Addresses that are not public (loopback, private ranges, cloud metadata) are refused, both when registered and at every delivery.

Jira is not connected to this application and no example creates tickets.
