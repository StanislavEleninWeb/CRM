# SEWEB CRM

A multi-tenant B2B sales CRM: evidence-based prospect research, a call-first daily queue, and a unified relationship history. This repository is public; it contains no real prospect data.

Status and next steps are in [docs/build-status.md](docs/build-status.md). Agents should start with [AGENTS.md](AGENTS.md).

## Requirements

Docker with Compose. Nothing else is installed on the host: Python, Node, PostgreSQL and Redis all run in containers.

## Start

```bash
make up
```

This copies `.env.example` to `.env` if needed, builds the images, applies migrations, and waits until every service is healthy.

| Service | Address |
|---|---|
| Web app | http://localhost:5173 |
| API | http://localhost:8000 |
| API documentation | http://localhost:8000/api/v1/docs |

PostgreSQL and Redis are reachable only from inside the Compose network.

Sign in with one of the development users, for example `owner@example.test`, password `dev-password`. These exist only in the bundled development identity provider (`infra/dex/config.dev.yaml`).

## Commands

| Command | What it does |
|---|---|
| `make up` / `make down` | Start or stop the stack, keeping data |
| `make reset` | Stop the stack and delete local data |
| `make migrate` | Apply migrations as the migrator role |
| `make test` | Backend tests against PostgreSQL as the runtime role |
| `make lint` / `make typecheck` | Ruff and mypy |
| `make frontend-check` | Frontend type-check, lint, tests and build |
| `make gen-api` | Regenerate `frontend/openapi.json` and the TypeScript client |
| `make check` | Everything CI runs |
| `make smoke` | Verify a running stack: readiness, frontend-to-API, worker job, scheduler tick |
| `docker compose exec api python /infra/e2e/phase_a.py` | End-to-end check of the call-first pilot on a fresh stack (`make reset && make up` first) |
| `./infra/backup/backup.sh`, `./infra/backup/restore-check.sh <file>` | Back up the database and verify the backup restores |

## Database roles

| Role | Used for | Privileges |
|---|---|---|
| `crm_owner` | Bootstrapping only | Superuser inside the container |
| `crm_migrator` | Migrations | Owns schema objects |
| `crm_app` | API and workers | No superuser, no `BYPASSRLS`, no DDL |

Tests migrate a separate `crm_test` database as `crm_migrator` and then connect as `crm_app`. Readiness fails if the runtime role could bypass row-level security.

## Reference workbook

Tests marked `reference_fixture` need the private workbook at `tests/fixtures/private/SEWEB_prospects_Bulgaria_2026-10-07.xlsx`. The folder is git-ignored. When the file is absent those tests are reported as skipped, never passed.

## Layout

```
backend/    FastAPI app, Alembic migrations, Celery worker
frontend/   React + Vite + TypeScript
infra/      Database bootstrap, smoke test, deployment configuration
docs/       Scope, decisions, backlog, status, evidence, policies
tests/      Shared fixtures (synthetic committed, private ignored)
```
