# Operations runbook

For whoever is on call. Each section says how the problem shows, what to check, and what to do. Commands assume the production stack; add `-f infra/production/compose.yaml` to `docker compose`.

## Monitoring

Scrape `http://api:8000/ops/metrics` from inside the host with `Authorization: Bearer $OPS_METRICS_TOKEN`. It is not served to the internet. Figures are totals over all workspaces and contain nothing that identifies one.

| Figure | Alert when | Meaning | Section |
|---|---|---|---|
| `crm_due_jobs_oldest_due_seconds` | > 300 for 10 min | Scheduled work is not being picked up | Poller |
| `crm_due_jobs_failed` | increases | A job gave up after its retries | Poller |
| `crm_sends_unknown` | > 0 for 1 h | An email's outcome needs a person | Unknown send |
| `crm_sends_oldest_unknown_seconds` | > 86400 | Nobody has settled it for a day | Unknown send |
| `crm_sends_stuck_dispatching` | > 0 | A worker died mid-send and recovery has not run | Poller |
| `crm_sends_waiting_overdue` | > 0 for 15 min | Due emails are not going out | Poller |
| `crm_mailboxes_sync_stale` | > 0 for 30 min | Replies are not being read | Gmail |
| `crm_mailbox_watches_expiring_24h` | > 0 for 6 h | Push renewal is failing | Gmail |
| `crm_webhook_endpoints_failing` | > 0 | A customer's receiver is down | Webhooks |
| `crm_webhook_deliveries_dead_24h` | > 0 | Events were given up on | Webhooks |
| `crm_research_runs_paused` | informational | Usually budget or a revoked key | Budget |
| `crm_regulatory_sources_expired` | > 0 | Unsolicited email is blocked in a workspace | Register |

Also watch: `/readyz` (database with a safe role, Redis), container restarts, disk space on the database volume, certificate expiry, and backup age (newest file in the backup directory older than 26 hours).

Logs are JSON on standard output, one line per request with a correlation ID. Secrets and URL query secrets are redacted. **No error-tracking service is connected**; choosing one is open. Until then, alert on log lines with `"level": "error"`.

## Poller, leases and the outbox

*Shows as:* due-row lag rising; emails not going out; research runs not advancing.

1. `docker compose ps` — is `scheduler` up (exactly one) and `worker` healthy?
2. `docker compose logs --since 15m scheduler worker | grep -E 'error|poll'`.
3. Redis reachable? `docker compose exec redis redis-cli ping`. If Redis was lost or flushed, nothing is lost: rows stay in PostgreSQL and are claimed again when their lease (two minutes) expires.
4. Restart the worker, then the scheduler. Work resumes from the database.

Stuck rows: a row is `claimed` with an expired lease only until the next poll. If `crm_due_jobs_failed` rose, list them as the migration role: `SELECT kind, last_error, count(*) FROM due_jobs WHERE status = 'failed' GROUP BY 1, 2;` Fix the cause, then set `status = 'pending', attempts = 0, due_at = now()` for those rows.

**Never** set a send intent from `dispatching` or `unknown` back to `queued`. That can send an email twice.

## Email whose outcome is unknown

*Shows as:* "Sends to check" on the Email page; `crm_sends_unknown`.

The application asked the mailbox provider to send and did not get an answer, or the worker died mid-send. It has looked for the message three times and will not send again.

1. A manager opens **Email → Sends to check**, looks in the mailbox's Sent folder for that recipient and subject, and records **It is in Sent** or **It was not sent**.
2. "Not sent" returns the draft for a new approval and a new request.

A failed (not unknown) send says why: disconnected mailbox, provider refusal, changed rules. Acknowledge it after acting.

## Gmail: token expiry, watch and history

*Shows as:* a mailbox marked degraded or revoked on Integrations; stale-sync or expiring-watch figures.

| Symptom | Cause | Action |
|---|---|---|
| Status `revoked`, "authorisation was withdrawn or expired" | The user removed the app, changed password policy, or the refresh token expired | An administrator reconnects under Integrations. History and position are kept; a full resynchronisation fills the gap |
| "Push notifications could not be renewed" | Pub/Sub topic or permission problem at Google | Replies are still collected every 15 minutes. Check the topic and that Gmail's push account may publish; the renewal retries hourly |
| Sync says "full sync" after an outage | The saved history position is older than Google keeps | Expected; it recovers by itself and stores nothing twice |
| Sync stale but mailbox active | Worker or poller problem | Poller section |
| Provider rate limiting | Too many calls | It backs off by itself for the time Google asks |

Nothing here needs database changes. Do not edit `history_cursor` by hand.

## Opt-out register out of date

*Shows as:* unsolicited drafts blocked with "register is out of date"; `crm_regulatory_sources_expired`.

The owner obtains the current register through its official channel and imports it under **Email → Outreach rules**. The application never fetches it and never treats a missing register as empty. Messages blocked meanwhile need a new approval.

## Research source data past its date

Undecided research candidates are deleted 30 days after they were found. If the terms of a data source change, an administrator changes that source's policy under Research; fields that may no longer be stored are dropped from new results. Existing stored fields are not rewritten automatically: export the affected candidates and decide.

## Budget exhausted

*Shows as:* research runs paused with a budget message.

Nothing is spent beyond the budget, and there is no paid overage. The owner raises the budget under Integrations and resumes the run, or waits for the next month. A reservation marked "unknown" keeps its amount reserved until an owner checks the provider's records and resolves it.

## Webhook deliveries failing

The customer's receiver is refusing or unreachable. Deliveries retry for about a day and then stop. Tell the customer; when it is fixed, an administrator presses **Send again** on the dead deliveries. A redirect is a failure by design.

## Subscription state looks wrong

The owner presses **Check with the billing provider now**. State is always read from the provider, never taken from an event. If it shows "a price this application does not recognise", the subscription was made for a price that is not attached to a plan: `python -m app.modules.billing.configure`.

## Encryption key rotation

See `docs/provider-setup.md`. One command re-encrypts provider credentials, mailbox tokens and webhook secrets. **Do not rotate `ERASURE_HASH_KEY`**: changing it un-erases every erased business.

## External Gmail assessment renewal

Not applicable until the gate in `docs/external-gmail-gate.md` is opened. When it is: put the assessment's expiry in `EXTERNAL_GMAIL_ASSESSMENT_VALID_UNTIL` and a calendar reminder 90 days before it. On that date the gate closes by itself and no new external mailbox can connect.

## Backups and restore

See `backup-restore.md`.
