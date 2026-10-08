# Test evidence

Only results that were actually run are recorded here, with the command, the date, and what the result does and does not prove.

## Phase 00

No application tests exist yet. The following checks were run against the reference workbook with a read-only script (openpyxl, cached values) on 8 October 2026, outside the application:

| Check | Result |
|---|---|
| Lead rows, columns, unique IDs | 94, 35, 94 |
| Tiers A / B / C | 21 / 62 / 11 |
| Confidence high / medium / low | 31 / 58 / 5 |
| Totals equal component sums; components within caps | All 94; none over cap |
| Tier thresholds at 80 and 60 | No mismatches; scores 59, 60, 79, 80 all occur |
| Shortlist | 25 rows, all IDs in master, equals top 25 by score, 21 A + 4 B, minimum 77 |
| Literal `Not found` by column | website 30, phone 1, email 58, contact page 46, other channel 41 |
| Phone cells with a semicolon | 17 |
| Raw website-status values; raw channel values | 14; 4 |
| Phone-first recommendations | 66 |
| Listing identifiers | 79 `place_id`, 15 `cid` |
| Emails; free-mail | 36; 19 (10 abv.bg, 8 gmail.com, 1 mail.bg) |
| Macros or external links | None |

**Limitation:** this confirms the fixture matches the build pack's assertions. It is not evidence that the application imports it correctly; that is the phase 04 gate.

## Phase 01 — 8 October 2026

All commands ran in containers on the local machine.

| Command | Result |
|---|---|
| `make up` (clean volumes) | All services healthy; migration `0001` applied from an empty database |
| `pytest` (backend, PostgreSQL 17, role `crm_app`) | 15 passed |
| `ruff check`, `ruff format --check`, `mypy app` | Clean |
| `alembic downgrade base && alembic upgrade head && alembic check` | Clean; no pending model changes |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 3 tests passed |
| `./infra/smoke.sh` | Readiness `ready`; frontend proxied `/api/v1/system/info` from the real API; worker returned the synthetic job result; scheduler recorded a due-row poll |
| Production image builds (`backend`, `frontend`) | Built; API image runs as uid 10001 |
| OpenAPI document vs generated | Identical |

What the tests cover: readiness succeeds with database and Redis and returns 503 when the database is unreachable; the runtime role is not a superuser, cannot bypass row-level security and cannot create tables; an unset tenant resolves to `NULL`; the error envelope and correlation IDs; log redaction; settings validation; tenant-local dates; the beat schedule contains only the short polling task.

**Limitations:** the GitHub Actions workflow has not run on GitHub (nothing is pushed); its steps were run locally by hand. No tenant-owned tables exist yet, so isolation is not demonstrated until phase 02.
