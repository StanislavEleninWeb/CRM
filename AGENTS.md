# Agent instructions — SEWEB CRM

This repository is **public**. Never commit real prospect contacts, secrets, or provider credentials.

## Before editing

Read, in order:

1. `docs/build-status.md` — completed phases, blocked gates, next action.
2. `docs/approved-scope.md` — requirements and reference mapping.
3. `docs/decisions.md` — confirmed decisions and unresolved choices.
4. `docs/task-backlog.md` — stable task IDs (`CRM-xxx`), dependencies, status.
5. `docs/test-evidence.md` — what was actually run and what it proved.

The build follows build-pack revision 2 (8 October 2026): call-first pilot, deterministic scoring in phase A, internal Gmail pilot with manual sends, email sequences deferred to phase 15.

## Hard rules

- Tenant isolation is mandatory everywhere. The API connects as the non-owner `crm_app` role (no `BYPASSRLS`); tests must run as that role. Missing tenant context fails closed. Never replace PostgreSQL isolation tests with SQLite.
- The reference workbook lives only in `tests/fixtures/private/` (git-ignored). Tests that need it use the `reference_fixture` pytest marker and must report **skipped**, never passed, when it is absent.
- Never invent contacts, evidence, budgets, completed calls, delivery confirmations, or test results.
- No live email, calls, paid provider usage, production deployment, or external tickets without explicit authorization.
- Schedules live in PostgreSQL due rows claimed with `FOR UPDATE SKIP LOCKED`. No multi-day Celery ETA/countdown tasks.
- Do not start phase 15 (sequences and optional integrations) implicitly.
- Task states: `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`. Provider status: `IMPLEMENTED`, `VERIFIED_LOCALLY`, `VERIFIED_IN_SANDBOX`, `VERIFIED_LIVE`.

## Layout

- `backend/` — FastAPI modular monolith, SQLAlchemy, Alembic, Celery.
- `frontend/` — React, Vite, TypeScript.
- `infra/` — Compose files, database role bootstrap, proxy configuration.
- `docs/` — scope, decisions, backlog, status, evidence, policies.
- `tests/fixtures/synthetic/` — committed synthetic fixtures. `tests/fixtures/private/` — ignored.

## Commands

See `README.md` for startup, test, and migration commands.
