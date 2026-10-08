# Build status

Resume from here after a context reset. Do not rerun completed phases.

## Current position

| Field | Value |
|---|---|
| Build pack | Revision 2, 8 October 2026 |
| Branch | `build/core` (local commits only; not pushed) |
| Last completed phase | 02 |
| Next action | Phase 03: CRM-030 companies, contacts, contact channels, leads and deals |

## Phases

| Phase | Status | Commit | Notes |
|---|---|---|---|
| 00 Scope, repository, decisions | DONE | see `git log` (`Phase 00`) | Docs only; no application code |
| 01 Executable foundation | DONE | see `git log` (`Phase 01`) | Tenant isolation and identity are not part of this phase |
| 02 Identity, tenancy, roles, isolation | DONE | see `git log` (`Phase 02`) | Production identity provider and MFA verification remain BLOCKED (U-01) |
| 03 Core CRM and history | TODO | — | |
| 04 Research schema, scoring, import/export | TODO | — | |
| 05 Prospect review, shortlist, calls | TODO | — | |
| 06 Provider connections and usage | TODO | — | |
| 07 AI research | TODO | — | |
| 08 Internal Gmail and eligibility | TODO | — | |
| 09 Reliable manual sends | TODO | — | |
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
