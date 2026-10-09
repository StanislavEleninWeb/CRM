# Build status

Resume from here after a context reset. Do not rerun completed phases.

## Current position

| Field | Value |
|---|---|
| Build pack | Revision 2, 8 October 2026 |
| Branch | `build/core`; pull request StanislavEleninWeb/CRM#2 holds phases 00–08 (first commit); later commits are local until the owner asks for a push |
| Last completed phase | 14. Phase 13 is prepared but not complete (see its row). Start from `release-checklist.md` |
| Next action | See "The smallest next actions" in `release-checklist.md`. First: the owner decides whether to push phases 09–14 |

## Phases

| Phase | Status | Commit | Notes |
|---|---|---|---|
| 00 Scope, repository, decisions | DONE | see `git log` (`Phase 00`) | Docs only; no application code |
| 01 Executable foundation | DONE | see `git log` (`Phase 01`) | Tenant isolation and identity are not part of this phase |
| 02 Identity, tenancy, roles, isolation | DONE | see `git log` (`Phase 02`) | Production identity provider and MFA verification remain BLOCKED (U-01) |
| 03 Core CRM and history | DONE | see `git log` (`Phase 03`) | Lead list and lead-detail screens arrive with the prospect workspace in phase 05 |
| 04 Research schema, scoring, import/export | DONE | see `git log` (`Phase 04`) | Reference-workbook gate passed locally; those tests are skipped in public CI |
| 05 Prospect review, shortlist, calls | DONE | see `git log` (`Phase 05`) | Phase A handoff check passed on the local stack |
| 06 Provider connections and usage | DONE | see `git log` (`Phase 06`) | Only local test adapters exist; no real provider has been contacted |
| 07 AI research | DONE except refresh mode | see `git log` (`Phase 07`) | Synthetic runs only. Live research BLOCKED (U-03, U-04). Open: refresh-existing mode (CRM-074), multi-page inspection |
| 08 Internal Gmail and eligibility | DONE | see `git log` (`Phase 08`) | Stand-ins only. Live Gmail BLOCKED (U-02); live unsolicited email BLOCKED (U-06). Nothing can be sent yet |
| 09 Reliable manual sends | DONE | see `git log` (`Phase 09`) | Sending is `off` by default. Verified with a fake mailbox only; no email has ever been sent |
| 10 Public API, webhooks, Hermes | DONE | see `git log` (`Phase 10`) | Webhook delivery verified against a mocked receiver only |
| 11 Subscriptions and entitlements | DONE except the Stripe sandbox run | see `git log` (`Phase 11`) | `BILLING_MODE=off` by default. Plans are labelled test plans; no price is approved (U-08). Stripe has never been contacted |
| 12 Reporting and operational controls | DONE with stated gaps | see `git log` (`Phase 12`) | No report filter by city or service; backup retention window open until hosting exists |
| 13 Staging and deployment pipeline | IN_PROGRESS | see `git log` (`Phase 13`) | Nothing deployed (U-07). Configuration, scripts, backups, monitoring and runbooks are in place; production images were not rebuilt or started in this session |
| 14 Release evidence and handoff | DONE | see `git log` (`Phase 14`) | Evidence is local, with stand-in providers. The checklist lists what is blocked and on whom |
| 15 Deferred extensions | Not run implicitly | — | |

## Repository audit (CRM-001)

- Path: `/Users/stanislavelenin/ProjectsSource/SEWEB/CRM`; remote `StanislavEleninWeb/CRM` (public).
- Before this build the repository held only `docs/discovery-review.md` (merged to `main`). There was no application code to reuse or migrate.
- No `AGENTS.md` existed; one was added.
- Host tooling: Docker 29.7 with Compose 5.4, Node 26, pnpm 11, Python 3.14 (not used for the app; the backend image pins Python 3.13). No local PostgreSQL or Redis; both run in containers.

## Source inputs

| Input | State |
|---|---|
| Build pack revision 2 | Read; summarised in `approved-scope.md`; not committed |
| Discovery proposal v2 | Read; background only; not committed |
| Reference workbook | Present at `tests/fixtures/private/` (git-ignored) |

## Blocked gates

See `release-gates.md` and the unresolved list in `decisions.md` (U-01 to U-10). None block local implementation and synthetic tests.

## Dependency versions (phase 01)

Pinned in `backend/uv.lock` and `frontend/pnpm-lock.yaml`. Base images `python`, `node`, `postgres`, `redis`, Dex and SeaweedFS are pinned by digest. **Pinned by tag only, still to be pinned by digest:** `ghcr.io/astral-sh/uv:0.12.23`, `nginxinc/nginx-unprivileged:1.29-alpine` and `caddy:2.10-alpine`.

| Component | Version |
|---|---|
| Python image | 3.13 slim |
| FastAPI / SQLAlchemy / Alembic | 0.142 / 2.1 / 1.20 |
| psycopg / Celery / redis-py | 3.3 / 5.6 / 6.4 |
| PostgreSQL / Redis | 17 / 7 |
| Node image / pnpm | 24 / 11.22 |
| React / Vite / TypeScript | 19.3 / 8.3 / 6.0 |
| TanStack Query / React Router | 5.104 / 7.18 |

TypeScript is held at 6.0 because typescript-eslint 8.71 does not support TypeScript 7.

## Conventions established in phase 02

- The schema is defined only by hand-written SQL migrations. Application code queries with SQL text or reflected tables (`app.core.db.table`); there are no ORM models to keep in sync.
- Every endpoint gets its database session from `UserSession` (acts as the user, no tenant) or `Tenant` / `tenant_with(permission)` (acts inside the active tenant). Both commit before the response is sent.
- Background jobs open their session with `app.worker.context.tenant_job(tenant_id, actor_user_id)`.
- Frontend data that belongs to a workspace uses `useTenantQuery`, which puts the workspace ID in the cache key.
- Identity tables use `ENABLE ROW LEVEL SECURITY`; tenant-owned business tables use `FORCE`. Pre-tenant operations go through narrow `SECURITY DEFINER` functions.
- If a migration that was already applied locally is edited, run `make reset` before `make up`.
- API, worker, scheduler and the migration job share one image (`seweb-crm-backend:dev`). Tests run Celery tasks inline; the real worker path is checked with scripts in `infra/e2e/` against a running stack.
- Anything that must happen later is a row in `due_jobs` (`app.worker.due.schedule`). The scheduler claims due rows through `due_jobs_claim()`; handlers are registered in `app/worker/due.py`. Never use Celery `eta` or `countdown`.
- Provider content passes through `SourcePolicy.storable()` before it is stored. Add a policy row before adding a source.
- After a real import the local development database holds real prospect data. It lives only in the local Docker volume; `make reset` removes it.
- Email: eligibility is decided only by `app.modules.email.eligibility.evaluate`; anything that sends must call it at send time, not rely on an earlier preview. Mailbox HTML is sanitised on the server and the frontend still shows plain text only.
- A Gmail notification is a trigger, never data: the cursor moves only after a page of history has been stored and committed.
- A send is a `send_intents` row with explicit states; `app.modules.email.dispatch` is the only code that calls the provider's send. A handler that does anything slow commits first, so no lock is held across a network call.
- `unknown` is never returned to `queued` by code; only finding the message in the mailbox or a person's recorded decision ends it. `dispatching` returns to `queued` in exactly one case: the provider answered with a definite "slow down" refusal, so the message was not sent.
- Lock order for anything touching a recipient's sends: the recipient advisory lock (`eligibility.lock_recipient`) first, then `send_intents`, `email_drafts`, `mailboxes`.
- Events are written with `app.core.outbox.emit` in the same transaction as the change.
- An API key authenticates with `Authorization: Bearer crm_…` and goes through the same `tenant_with(permission)` checks as a person; `app.core.apikeys.ALLOWED_SCOPES` is the complete list of what a key can hold. An endpoint that records a person's judgement or loosens a restriction under one of those scopes must call `ctx.require_person(...)`; `test_a_key_with_every_scope_still_cannot_make_a_persons_decisions` lists them. New endpoints need no extra work to be key-safe, but a new permission is not available to keys until it is added there on purpose.
- Anything written for a request made with a key is attributed through `session.info["api_key_id"]`, not through request-local context (dependencies and handlers run on different worker threads).
- New event types are added to `webhooks.KNOWN_EVENTS` and emitted with `outbox.emit` inside the transaction that makes the change.
- Whether a workspace may act is decided by `billing.entitlements.evaluate`. It is called in `get_tenant_context` for every state-changing request (people and API keys), and again by the research step and the send dispatcher. A new worker that spends money or sends anything must call it too.
- Billing state changes only through `billing.service.sync`, which reads the provider. Event payloads and browser redirects are never applied.
- A new countable resource gets an entry in `entitlements.LIMITS` and a `require_room` call where it is created.
- `plans` is seeded by migration and is not cleared between tests.
- A figure in a report is an entry in `reports.router.RECORDS` (the same query gives the count and the list behind it) plus a `Metric` with its definition. A rate goes through `_rate`, which returns "no data" for a zero denominator.
- Anything that can create a company, lead or message from outside (import, research, mailbox) must ask `dataops.service.is_erased` first.
- Support access is not a membership and is an allow-list: `app.core.access_policy.SUPPORT_READABLE` and `SUPPORT_COMMUNICATIONS`. A new read route is closed to support until it is added to one of them; a test sweeps every GET route.
- What a workspace may still do when its subscription is not in good standing is the allow-list `ALLOWED_WHEN_RESTRICTED`: reducing access, stopping contact, removing data, billing. Never add something that creates or sends.
- Hosted environments use `infra/production/compose.yaml` with images by digest; migrations run only as the one-off `migrate` job from `infra/deploy/deploy.sh`. The development `compose.yaml` is never used for hosting.
- Monitoring reads `ops_snapshot()` through `/ops/metrics`. A definer function that reads `send_intents`, `mailboxes`, `webhook_endpoints`, `webhook_deliveries`, `research_runs` or `regulatory_sources` must not rely on row security alone while `app.ops_snapshot` could be set: filter by tenant explicitly.
