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

## Phase 04 — 8 October 2026

| Command | Result |
|---|---|
| `pytest` with the private reference workbook present | 144 passed, 0 skipped |
| `pytest` with the fixture directory absent | Reference tests reported as `SKIPPED: reference fixture absent` (5), the rest passed |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 15 tests passed |
| Real stack, real worker (`infra/e2e/client.py`): upload, parse, commit, re-import, export of the reference workbook | Parsed in about 1 s and committed in about 1 s by the Celery worker: 94 created; re-import 94 unchanged with the same shortlist; export returned a workbook |

### Reference workbook gate (the phase 04 acceptance assertions)

All verified by `tests/test_import.py` against the actual file, through the upload API, as the runtime database role:

| Assertion | Result |
|---|---|
| Leads created | 94 (not 119); 94 distinct Lead IDs; 94 companies |
| Shortlist | 25 entries, all linked to existing leads, creates no leads; 21 A + 4 B fillers; minimum score 77; equals the top 25 by score; dated 2026-10-08 |
| Tiers | 21 A / 62 B / 11 C, computed on the server |
| Component totals | All match the source totals; no component above its cap; scores 59, 60, 79, 80 fall in C, B, B, A |
| Confidence | 31 high / 58 medium / 5 low |
| Columns | 35 mapped, none unmapped; every lead keeps a 35-key raw row |
| Phone cells with a semicolon | 17; one `delivery` and one `emergency` number typed as such; the emergency number is not usable for sales calls; 93 companies have a phone; every number normalised to international form (country known) |
| Website status | 14 raw values kept; none left unclassified; 30 "no own website" |
| Recommended channel | 4 raw values; 66 phone-first (one with the instruction "ask for the manager"), 28 email-first (one with a phone fallback) |
| Missing markers | 176 literal `Not found`: website 30, phone 1, email 58, contact page 46, other channel 41; none stored as a contact value |
| Listing identifiers | 79 `place_id`, 15 `cid` (numeric, not reinterpreted) |
| Emails | 36; 19 on free-mail domains (10 abv.bg, 8 gmail.com, 1 mail.bg); no legal classification inferred |
| Neither website nor email | 30 leads |
| Tags | 10 distinct, including `Other` on 2 leads |
| Date checked | Stored as `DATE` 2026-10-07; identical when read in UTC, Europe/Sofia, America/Los_Angeles and Pacific/Kiritimati sessions, and in the export |
| Provenance | Every assessment is `user_import` / `unverified` |
| Re-import | 94 updates, 0 creates; no new assessments, scores, observations or shortlists |
| Round trip | Export matches the source cell for cell on 32 of 35 columns (dates compared as dates; the phone and other-channel columns are compared structurally); importing the export into a second tenant yields identical structured data for all 94 leads |
| Export summary | Calculated from exported rows: 94 total, 21/62/11, 93 with phone, 36 with email, 64 with a website, 25 on the shortlist of which 4 below Tier A |

### Synthetic cases (always run, including in CI)

Preview changes nothing until commit; a row becomes structured records with raw values kept; re-import adds a new research snapshot while keeping the old one, and preserves a do-not-contact flag, outreach status, edited company fields and a reviewer's score override; duplicate Lead IDs, over-cap and partial scores, and a missing name are errors and are skipped; a wrong source total is a warning and the computed total wins; an unscored row imports unscored; text beginning with `=`, `+`, `-` or `@` is imported and exported as text with no formula in the output XML; formulas are never evaluated and cells without a cached result are counted; `.xlsm`, `.xls`, empty, oversized, non-zip and macro-carrying files are refused; CSV with Cyrillic and a possible-duplicate review; column mapping can be corrected; import and export need permission and stay inside the tenant; a duplicate task delivery does not apply an import twice; score overrides are computed on the server and keep history.

**Limitations:**

- The reference gate is verified only where the private workbook is present. Public CI reports those five tests as skipped.
- Re-import fills blank company fields and adds new channels; it does not overwrite company fields a user may have edited. A deliberate "overwrite from file" option does not exist.
- Rows without a Lead ID are matched only by exact name and city; fuzzy matching is left to the duplicate review on the company page.
- The export's shortlist sheet is the current top 25 by score. The call-first daily queue arrives in phase 05.
- A defect found and fixed while running the real stack: the worker and scheduler were built as separate images and had gone stale. They now share one image with the API.

## Phase 05 and the phase A handoff — 8 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 156 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 18 tests passed |
| `docker compose exec api python /infra/e2e/phase_a.py` on a fresh stack | 41 checks passed, using the real Celery worker and the reference workbook |
| `./infra/backup/backup.sh` then `./infra/backup/restore-check.sh` | Restore check passed: 35 tables, 2,792 rows, 43 policies, row-level security intact |
| Browser at 375 px width, real data | Today queue and prospect page: no horizontal scrolling, smallest tap target 44 px |

What the tests cover:

- **Ranking:** score, then confidence, then freshest check, then Lead ID; unscored prospects sort last and show no score; a score override re-ranks immediately; every filter (city, industry, service, issue tag, tier, confidence, freshness, channel, status, search).
- **Verification queue:** a Tier A lead with an unverified finding, a low-confidence lead and a lead checked more than 30 days ago are queued; a normal Tier B lead is not; verifying records who and when and refreshes the check date; contradicting needs a note; the freshness window is a tenant setting.
- **Reviewed edits:** the opening and instruction can be edited; the timeline keeps before and after; a later re-import refreshes the finding but keeps the reviewer's wording; findings themselves cannot be edited through this route.
- **Dismissal:** needs a reason, hides the prospect from the working list and the queue, removes dial links, and can be undone.
- **Call queue:** due follow-ups first, then phone-first prospects by score, then other callable prospects; Tier B entries are flagged as fillers; a prospect with no phone or only an emergency line is not queued; a short list says how many were eligible and is not padded; the score-ranked view is separate; queue size is a tenant setting.
- **Snapshots:** dated in the tenant time zone (checked at UTC+14 and UTC−11); "tomorrow" is a date, never a label; a snapshot keeps the score as ranked that day beside the current score; repeat generation returns the same snapshot unless asked to regenerate; snapshots add no leads.
- **Calls:** starting a call records only that the dialler was opened; outreach status and the timeline change only when the user reports an outcome; outcomes are a fixed list and a duration is not accepted; a second outcome for the same attempt is refused; calls made elsewhere are logged without a dialler launch.
- **Follow-ups:** a requested follow-up needs a date, creates a stored call task, records what was asked for, and can store a business email given on the call as an unverified channel; the email action stays unavailable without a mailbox.
- **Do not call:** a wrong number becomes invalid and loses its dial link; "not interested" with a do-not-call request creates an audited restriction and removes the prospect from the queue; emergency lines and restricted companies are refused with a clear reason.
- **Permissions and isolation:** read-only members see prospects without dial links and cannot call, edit, verify, dismiss or generate lists; another tenant gets 404 for prospects, snapshots, calls, findings and hypotheses.
- **Reference workbook:** 94 prospects; all 21 Tier A leads wait for verification; 5 are low-confidence; 93 can be called and 1 has no phone; the score-ranked list equals the imported shortlist; the call queue holds 25 phone-first prospects and intentionally differs from the score-ranked list.

### Defects found by running the real stack, and fixed

- Prospect cards overflowed the screen at 375 px (long findings and badges). Fixed in CSS and re-checked: page width equals viewport width.
- The first restore attempt failed because a scratch database lacked the role and extension preparation a new environment gets. The role script now accepts database names, and the runbook uses it.

**Limitations:**

- The handoff check and the restore exercise ran on a developer machine. A hosted pilot still needs host-specific security and deployment checks (U-07); this is a local prototype, not a production deployment.
- Calls are user-reported. Nothing confirms that a call connected or how long it lasted.
- The verification queue rules are fixed (low confidence, stale, contradicted, unverified Tier A); only the freshness window is configurable.
- Lead-level notes are added on the company page; the prospect page links there.
- Accessibility was checked through semantic queries in tests (roles, labels, names) and tap-target size; no screen-reader session or automated contrast audit was run.

### Phase 05 corrections after review — 8 October 2026

| Finding | Fix | Evidence |
|---|---|---|
| Real phone numbers, a place ID and a listing ID from the private workbook had been copied into tests and code comments in the unpushed commits | Replaced with synthetic values in every commit of `main..build/core`; old objects pruned. Nothing had been pushed. | A scan of tracked files and of that history against 620 identifying workbook values finds none. `tests/test_no_private_data.py` repeats the scan on every local run. |
| The call queue did not move on: a prospect just spoken to, or with a follow-up booked for later, stayed at its rank | Queue rules added (see `decisions.md`) and shown on the Today page | `test_call_queue_moves_on_as_calls_are_reported`; added to `infra/e2e/phase_a.py` |
| An earlier version of this document said lead conversion was available on the company page. It was not available in any screen. | Owner assignment and "Convert to opportunity" added to the prospect page | Frontend test; conversion added to `infra/e2e/phase_a.py` |
| The export showed the outreach status and recommended channel as imported, even after they changed | The export shows current values and uses the imported wording only while it still means the same | `test_export_shows_the_current_status_and_channel_not_the_imported_text` |

After these fixes: backend 159 passed (reference workbook present), frontend 19 passed, phase A end-to-end check passed on a fresh stack.
