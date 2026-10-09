# Build status

Resume from here after a context reset. Do not rerun completed phases.

## Current position

| Field | Value |
|---|---|
| Build pack | Revision 2, 8 October 2026 |
| Branch | `build/core`; pull request StanislavEleninWeb/CRM#2 holds phases 00–08 (first commit); later commits are local until the owner asks for a push |
| Last completed phase | 09 (releases A and B implemented; B live gates blocked) |
| Next action | Phase 10: scoped API keys, idempotency, signed webhooks from the outbox, Hermes examples |

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
| 10 Public API, webhooks, Hermes | TODO | — | |
| 11 Subscriptions and entitlements | TODO | — | |
| 12 Reporting and operational controls | TODO | — | |
| 13 Staging and deployment pipeline | TODO | — | |
| 14 Release evidence and handoff | TODO | — | |
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

Pinned in `backend/uv.lock` and `frontend/pnpm-lock.yaml`; base images pinned by digest.

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
