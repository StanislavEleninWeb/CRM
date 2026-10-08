# Build status

Resume from here after a context reset. Do not rerun completed phases.

## Current position

| Field | Value |
|---|---|
| Build pack | Revision 2, 8 October 2026 |
| Branch | `build/phase-00` (local commits only; not pushed) |
| Last completed phase | 01 |
| Next action | Phase 02: CRM-020 identity and sessions, then tenants, roles and row-level security |

## Phases

| Phase | Status | Commit | Notes |
|---|---|---|---|
| 00 Scope, repository, decisions | DONE | see `git log` (`Phase 00`) | Docs only; no application code |
| 01 Executable foundation | DONE | see `git log` (`Phase 01`) | Tenant isolation and identity are not part of this phase |
| 02 Identity, tenancy, roles, isolation | TODO | — | |
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
