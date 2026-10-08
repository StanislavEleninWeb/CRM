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

## Phase 01 addendum — clean checkout

`git clone` of the local repository into an empty directory, then `make up && make smoke`: all services healthy and the smoke test passed. This confirms the documented commands work without pre-existing `.env`, `node_modules` or volumes.

## Phase 02 — 8 October 2026

| Command | Result |
|---|---|
| `pytest` (PostgreSQL 17 as `crm_app`, Redis, Dex 2.46 as the identity provider) | 68 passed |
| `ruff check`, `ruff format --check`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test` | Clean; 11 tests passed |
| Browser walk-through on `localhost:5173` | Sign-in through Dex, workspace creation, Team page at 375 px width |

What is covered:

- **Sign-in:** the full authorization-code flow against a real OIDC server; state bound to the browser; callback replay; PKCE verifier mismatch (rejected by the provider); open-redirect attempts on the return path.
- **ID token validation** with test-controlled keys: wrong issuer, wrong audience, expired, nonce mismatch or missing, missing subject or email, unknown signing key, unsigned (`alg=none`), HMAC signed with the public key, multiple audiences with a foreign `azp`, discovery document with an unexpected issuer.
- **Sessions:** token stored only as a hash; logout, expiry, revoking other sessions; one user cannot revoke another's session; CSRF token required on unsafe methods.
- **Isolation through the API:** two workspaces with the same name; forged tenant headers and query parameters ignored; switching to a non-member workspace refused; cross-tenant updates and deletes return 404; a member of two workspaces sees only the active one.
- **Isolation in the database, as `crm_app`:** no context means zero rows and rejected inserts; cross-tenant inserts rejected by policy; cross-tenant updates and deletes match nothing; a row cannot be moved to another tenant; the composite foreign key refuses a link to another tenant's member; the runtime role cannot set the platform-operator flag, rewrite audit events, or insert tenants directly.
- **Connection pool:** with a pool of one connection, context set in a failed transaction is gone on the next checkout; context survives a commit inside one unit of work.
- **Definer functions:** need the exact token hash; expose no identifiers; refuse to create a tenant without a user.
- **Invitations:** token shown once and stored hashed; single use; expired, revoked, unknown and wrong-email cases; a removed member loses access on the next request.
- **Roles:** monotonic permission matrix; read-only, representative and sales manager cannot manage members or settings; nobody can escalate their own role; administrators cannot create, change or remove owners; the last owner cannot be removed or demoted, enforced by a database trigger as well.
- **Transactions:** a failed commit produces a 500, not a success.
- **Background jobs:** tenant required; unknown tenant refused; actor membership re-checked at execution, including removal after enqueue.
- **Frontend:** signed-out state; onboarding with CSRF header; permission-based hiding; one-time invitation link; a delayed response from the previous workspace is not rendered after switching.

**Limitations:**

- Production identity provider is not chosen (U-01). The development provider does not report MFA, so MFA is recorded only as "reported by provider or not"; nothing enforces it yet.
- Cross-tenant link tests currently cover the one tenant-aware foreign key that exists (invitation inviter). CRM relationships arrive in phase 03 and get the same tests there.
- Invitation links are shown to the inviter to send by hand; the application sends no email.
- There is no sign-in rate limiting yet; it is provided by the identity provider.

## Phase 03 — 8 October 2026

| Command | Result |
|---|---|
| `pytest` (PostgreSQL as `crm_app`, Redis, Dex, SeaweedFS S3) | 89 passed |
| `ruff check`, `ruff format --check`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 14 tests passed |
| Migration `0003` upgrade, downgrade to `0002`, upgrade | Clean |

What is covered:

- **Main journey as a representative:** company, contact, phone and email channels, lead, qualification, conversion to an opportunity, stage changes with history, next action, note, task, tags, and the resulting timeline.
- **Leads and deals stay separate:** a lead needs no named person; disqualifying needs a reason; a converted lead cannot be converted again; a lost opportunity needs a reason.
- **Money:** tenant currency (EUR) is the default; conversion provenance must be complete; negative and over-precise amounts are rejected.
- **Phone handling:** raw value kept; international form only when the country is known; emergency numbers get no dial link; delivery and booking purposes are kept.
- **Restrictions:** a do-not-contact flag needs a reason, cannot be erased by deleting the channel, and only a manager can lift it; a company-level restriction blocks existing and later-added numbers; lifted restrictions stay in history; both are audited.
- **Permissions:** read-only members cannot change any CRM record; representatives cannot delete, bulk-edit, manage pipelines, or assign records to others.
- **Bulk actions:** preview by default, report missing IDs, and are reversible (archive and unarchive).
- **Lists:** 230 rows paged without overlap; case-insensitive filters; search wildcards treated literally; page size capped; an injected sort value is rejected.
- **Duplicates:** suggestions by shared phone, shared domain, and similar name in the same city; a same-domain record in another city is flagged as a possible branch; nothing merges automatically.
- **Merging:** contacts, leads, deals, tasks, notes, files, tags, timeline and restrictions all move to the surviving company; a shared phone keeps the stricter do-not-contact state; blanks are filled without overwriting; a repeat merge is refused.
- **Files:** stored in S3-compatible storage under a tenant prefix; path components stripped from names; type and 20 MB size limits; downloads streamed by the API with `nosniff`, `attachment` and `no-store`.
- **Isolation through the API:** every CRM read, write, nested create, merge and conversion against another tenant's record returns 404; linking one's own record to another tenant's record returns 422; bulk actions match nothing.
- **Isolation in the database, as `crm_app`:** nine composite foreign keys refuse cross-tenant links; a cross-tenant insert is refused by policy; timeline entries cannot be rewritten and restrictions cannot be deleted.
- **Schema guard:** every table that has a `tenant_id` column must have it `NOT NULL`, row-level security enabled and forced, and at least one policy. This test will fail for any future table that forgets.

**Limitations:**

- Duplicate detection compares one company against up to 500 others on demand; there is no background duplicate scan.
- Uploaded files are checked by declared content type and size only; there is no malware scanning.
- Storage was verified against SeaweedFS locally; no cloud S3 provider has been exercised.
- The lead list and lead actions exist in the API; their screens come with the prospect workspace in phase 05.
