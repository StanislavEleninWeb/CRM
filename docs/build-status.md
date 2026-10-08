# Build status

Resume from here after a context reset. Do not rerun completed phases.

## Current position

| Field | Value |
|---|---|
| Build pack | Revision 2, 8 October 2026 |
| Branch | `build/phase-00` (local commits only; not pushed) |
| Last completed phase | 00 |
| Next action | Phase 01: CRM-010 scaffold the FastAPI app, session layer and Alembic |

## Phases

| Phase | Status | Commit | Notes |
|---|---|---|---|
| 00 Scope, repository, decisions | DONE | see `git log` (`Phase 00`) | Docs only; no application code |
| 01 Executable foundation | TODO | — | |
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
