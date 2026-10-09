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

## Phase 06 — 8 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 176 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 20 tests passed |

What is covered:

- **Encryption:** AES-256-GCM; a secret opens only with the same tenant, provider and connection it was sealed for; a flipped byte fails; the same secret sealed twice differs.
- **Copied ciphertext:** one tenant's stored ciphertext written into another tenant's row cannot be decrypted; the victim connection reports "cannot be read" and the original still works.
- **Key rotation:** old secrets stay readable while the old key is listed; the rotation script re-seals every tenant's credentials; after the old key is removed the re-sealed credentials still work and un-rotated ones are reported.
- **Write-only credentials:** no response, list, audit entry or log line contains a realistic key; the database holds only ciphertext and a four-character hint. A scan of the OpenAPI document finds no read schema with a secret-like field.
- **Log redaction:** a defect was found here and fixed. A key passed in a URL query string (`?key=…`, `&access_token=…`, `&code=…`) was logged in full. Query-string secrets are now redacted, with a test.
- **Health:** failures back off (longer after each failure), rate-limit responses are recorded, a rejected credential marks the connection revoked and later checks fail visibly; reconnecting reuses the same record and clears the failure state; revoking erases the stored key.
- **Budgets, concurrency:** 20 threads, each on its own database connection, race for a budget that fits 10 reservations: exactly 10 succeed and the held total equals the limit. Eight threads using the same idempotency key produce one reservation.
- **Budgets, lifecycle:** no budget means paid work is refused; settling returns the unused part and writes a ledger entry with its cost basis; settling twice changes nothing; a timeout becomes `unknown`, keeps the budget held, cannot be released by code and must be resolved by an owner (charged with an amount, or not charged); a real charge above the reservation is recorded in full, marks the budget overrun and blocks further work until the limit is raised; the concurrent-run limit is enforced.
- **Usage:** provider-confirmed and estimated amounts are reported separately and never added together; held and unknown amounts are shown.
- **Permissions and isolation:** only owners and administrators manage connections; only owners set budgets; managers can read spend; another tenant gets 404 or an empty list; the ledger cannot be updated or deleted by the application role.
- **Environment guard:** local test adapters are not offered when the environment is staging or production.

**Limitations:**

- The only adapters are `fake_model` and `fake_discovery`, clearly labelled as local test adapters. No real provider has been contacted and no cost figure here is a real charge.
- OAuth connections are modelled (`access_mode = oauth`) but the first OAuth flow arrives with Gmail in phase 08.
- Monthly budgets are per calendar month in the tenant time zone; there is no automatic carry-over or alerting before the limit is reached.
- Encryption keys come from an environment variable. A managed key service is not integrated.

## Phase 07 — 8 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 243 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 21 tests passed |
| `docker compose exec api python /infra/e2e/phase_b_research.py` on a fresh stack | 12 checks passed through the real scheduler and worker, with local test adapters |

**Status of providers: no real provider was contacted and nothing was paid for.** The Google Places adapter is contract-tested against a mocked HTTP transport only (`IMPLEMENTED`). The model adapter is a local stand-in; no model provider has been chosen (U-03). Live research is `BLOCKED`.

What is covered:

- **Safe fetching.** Refused before any network activity: non-http schemes, credentials in the URL, literal private, loopback, link-local and metadata addresses in IPv4 and IPv6 (including IPv4-mapped and 6to4 forms), internal host names and unusual ports. A host with any internal address among its answers is refused. Over real sockets against a local server: the connection goes to exactly the address that was checked; a name that answers publicly when checked and internally afterwards (DNS rebinding) cannot move the connection; every redirect hop is re-checked (internal IP, internal name, `file:` and loops all stop); only HTML is read, up to 1.5 MB; a failed fetch is recorded as a limitation of the automated request, never as "the site is down".
- **Extraction** reads markup only: scripts and styles are ignored; title, language, phone and email links, booking and contact links, copyright year.
- **Source policy.** With the default policy only a listing's place ID is stored; name, website, address, phone, rating and review count are dropped and named in `dropped_fields`. An unknown source is denied. A source cannot be made storable without being marked approved with a note saying what was verified; a database constraint enforces the same. A dump of all stored candidates contains none of the provider's names, addresses or ratings.
- **Identity from the business's own site.** A candidate's stored name and website come from the page that was fetched. An unreadable site leaves the candidate with a place ID and nothing else.
- **Model output validation.** An observation must quote text that is on a fetched page; a contact must literally appear on a fetched page; scores must fit the rubric (a partial score is not zero-filled); the service must be in the tenant's catalogue; unknown fields such as `actions` are ignored and reported; confidence is lowered when evidence is missing.
- **Prompt injection.** A page instructing the model to maximise scores, email customers and reveal its key, with a stand-in model that obeys it: every injected element is rejected, the candidate goes to review, and no lead, task or message is created.
- **A synthetic run:** four searches, seven listings, with the expected outcome for each (qualified, rejected for low score, unreadable site, no website, excluded domain not even fetched, ambiguous website match); summary with counts, score and confidence distributions, estimated cost labelled as an estimate, unreadable sites, and unsupported checks (mobile layout, visual defects, page speed).
- **Not padded:** 2 of 10 requested candidates qualified and the summary says so.
- **Promotion** is the human approval step: it creates the company, lead, channels with their source page, observations, hypotheses kept separate, and a score recomputed from components even when the stored total was tampered with. A business with no readable website can only be added when a person confirms it and supplies the name.
- **Repeat runs:** an existing lead is recognised and its outreach status untouched; known candidates are neither fetched nor paid for again.
- **Limits and controls:** stops at the qualified target and at the cost cap, listing the searches not run; pauses when the budget runs out and resumes when it is raised; pause does nothing further; cancel keeps costs already incurred and leaves nothing reserved; a provider timeout leaves that cost `unknown` and the run continues; a revoked connection pauses the run with a clear message.
- **Scheduling:** a recurring set-up is a due row in the database with the next start in the tenant's zone (06:00 local stays 06:00 across the October clock change); running it creates a run and stores the next start; eight concurrent pollers claim 40 due rows exactly once each; a worker whose lease expired cannot act; a row delivered twice runs once; a failing handler is retried later with the delay stored in the database.
- **Retention and export:** undecided candidates past their date are deleted, promoted ones kept; contact values from a source that does not allow export are withheld from the workbook and counted.
- **Permissions and isolation:** representatives cannot start or cancel runs; another tenant sees nothing and cannot use this tenant's connections.

**Limitations:**

- **Refresh-existing mode is not built.** The API refuses it with a clear message. CRM-074 stays open for this.
- Inspection reads the homepage only. The `site` depth setting is accepted but behaves like `homepage`.
- `robots.txt` is not consulted yet.
- Website-to-listing matching is a word-overlap check on the page title and text; anything uncertain goes to review.
- The Places adapter's cost figure is a list-price ceiling entered by hand; it must be checked against current pricing before a live run.
- Google Places terms for an EEA-billed account are still unverified (U-04), so nothing beyond the place ID is stored and review counts are not used for scoring.

## Phase 08 — 9 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 274 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 26 tests passed |

**No real mailbox or Google account was contacted and no email was sent.** Google's token and key endpoints are replaced by a local signer; the mailbox is a fake; the Gmail HTTP adapter is contract-tested against a mocked transport (`IMPLEMENTED`). There is no code path that sends a message yet.

What is covered:

- **Eligibility.** An unsolicited draft is blocked until a mailbox exists, the policy is approved, the sender is identified, a current register is on file and the recipient is classified with evidence; unknown recipients go to review. A register match, a stale register, a private individual, a consumer context and a sole trader each give the expected outcome. Suppression by address or domain and a company-level email restriction always block, including after re-import. Suppressions cannot be deleted by the runtime database role.
- **Required content.** Editing a draft cannot remove the label, sender identification or opt-out link; an empty label blocks; a recipient containing a line break is refused.
- **Requested follow-up.** Allowed only after a call outcome recorded the request, and it does not make an unsolicited message to the same address allowed.
- **Policy approval.** Owner only, needs a sender identity and a substantive note, creates a new version, is audited.
- **Opt-out links.** Tampered or swapped tokens are rejected; a GET changes nothing; a POST suppresses once and is idempotent.
- **Connecting Gmail.** Only `gmail.send` and `gmail.readonly` are requested. Refused: another organisation, a consumer account, a spoofed address suffix without the organisation claim, an unverified address, wrong audience, wrong issuer, expired token, wrong nonce, missing or reused state, a callback finished by a different user, missing read permission, no long-lived token, a member without the integration permission, and any workspace other than the configured internal one — including with the external flag switched on. The stored token is encrypted and absent from API responses and the audit log.
- **Synchronisation.** The first sync is resumable and takes its cursor from before listing began; an incremental sync that dies mid-way keeps the pages already stored and re-reads the rest without duplicates; an expired cursor (404) falls back to a full sync; a notification with a far-future history ID does not move the cursor; duplicate and unknown-mailbox notifications are ignored; forged notifications are refused; lost notifications are recovered by the 15-minute check; one sync per mailbox; provider back-off is respected and shown.
- **Watch renewal.** Never adopts the renewal's history ID; a failure within 24 hours of expiry raises a visible alert; retried within the hour.
- **Withdrawn authorisation** is shown with a reconnect instruction; reconnecting keeps the cursor and history.
- **Conversations.** Replies link by thread; an address shared by two companies or an unknown sender waits for a person; automatic replies do not count as replies; incoming HTML is stripped of scripts, handlers, forms, frames, styles and remote images; attachment bodies are never requested; a permanent bounce suppresses the address and a temporary one does not.
- **Isolation.** Another workspace sees no mailboxes, threads, drafts or suppressions and cannot act on them by ID; a read-only member cannot draft or suppress.

**Limitations:**

- The Bulgarian policy is a draft based on an unofficial 2019 consolidation (U-06). The software enforces whatever the owner approves; it does not establish that the rules are correct.
- The register format is one address per line. The real register's format and access terms are unknown.
- The fake mailbox models Gmail's documented history behaviour; differences in the real service would only show at the live gate.
- The frontend shows message text only; the sanitised HTML is stored but not rendered.

## Phase 09 — 9 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 291 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 29 tests passed |
| Lint, type-check and `pytest` as a container user that cannot write to the source tree | Clean; 291 passed (the condition that failed on GitHub) |
| `alembic upgrade head`, `downgrade base`, `upgrade head` on a scratch database | Clean through revision 0009 |
| GitHub Actions on pull request 2 (phases 00–08) | frontend and images passed; backend failed at lint because the linter could not write its cache in the mounted source tree. Caches now go to `/tmp`; not yet re-run on GitHub |

**No email has been sent.** `EMAIL_DISPATCH` is `off` by default; the tests switch it on against a fake mailbox inside the test process. The dispatcher was exercised in-process with real PostgreSQL and concurrent threads, not through the separately running worker container (the fake mailbox lives in one process's memory). The scheduler-to-worker path for due rows in general was verified in phase 07.

What is covered:

- **The ordinary path.** Not approved: refused. Approved and requested: one row, nothing sent by the request. The due row is claimed, the message is built with the label, sender identification, opt-out link and one-click headers, handed to the mailbox once, and recorded as accepted. Asking again returns the same request. The sent message is attached to its prospect and a reply is recorded as delivery evidence.
- **Off and dry run.** Off refuses requests. A dry run passes every check, never calls the provider, and returns the draft for a fresh approval. A request made live is stopped if sending is switched off before it is due.
- **Approval binding.** Editing the text, subject or recipient withdraws approval. Changing the sender identification after approval blocks the request; the same change between request and dispatch blocks at the last check.
- **Review.** A recipient needing review cannot be approved without a note; a review reason nobody looked at appearing later blocks at dispatch; a block cannot be approved, and the refused attempt is recorded.
- **Before dispatch.** An opt-out through the signed link, a manual suppression, a register that became stale and a reply from the recipient each stop a waiting unsolicited message. A reply being written in the same conversation is not stopped, and is sent without the unsolicited label or unsubscribe headers.
- **Lock order.** With the recipient lock held elsewhere, the final check waits without holding the send row, so the opt-out can proceed; whichever commits first wins. The real opt-out endpoint raced against a send five times: the opt-out always returned 200 and was recorded. (A review found the original order could deadlock and abort the opt-out; it was reversed.)
- **Lost poller.** A due row claimed by a poller that died before publishing its task is claimed again after the lease and sent once; the first claim's late task is rejected as stale.
- **Cancellation.** A waiting message can be cancelled; one already sent cannot, and says so.
- **Exactly once.** One poller gets the due row. Eight simultaneous workers — half as the same task delivered again, half as direct calls — produce one send and one activity.
- **No lock across the network.** While the provider call is in flight, another connection locks the intent, draft, mailbox and due rows with `NOWAIT`, and PostgreSQL reports no idle-in-transaction session. An opt-out arriving at that moment is recorded and the in-flight message completes, as documented.
- **Ambiguous outcomes.** A timeout after which the message did land is found by its message ID and recorded, with one send call in total. One that did not land is checked three times at growing intervals stored in the database, then waits for a person; it is never retried, a repeated request returns the same row, and only a manager can record the outcome. "Not sent" returns the draft for a new approval and a new request; "sent" closes it.
- **Worker death.** After claiming: another worker takes over and sends once; the first worker's stale lease can neither send nor overwrite the result. After the point of no return: recovery marks it unknown and looks in the mailbox, without sending; a late success from the dead worker is still recorded.
- **Limits.** The daily limit and the spacing between messages delay a message by rescheduling it in the database. A provider "slow down" is retried later a bounded number of times, then fails.
- **Disconnected mailbox.** A withdrawn authorisation fails the send plainly, marks the mailbox, and a failure cannot be recorded as sent.
- **Chosen time.** A wall-clock time is interpreted in the workspace zone: 09:00 before and after the 25 October 2026 change map to different instants; a repeated hour means its first occurrence; a time that does not exist is refused; past times and more than 60 days ahead are refused. A scheduled message is a pending due row and is not claimed early.
- **Roles and isolation.** A representative can request but not approve; a read-only member neither; another workspace gets 404 on every action, and a worker acting for another tenant cannot see the message.

**Limitations:**

- Reconciliation relies on searching the mailbox by message ID. If Gmail's search lags or behaves differently from the fake, more sends would end as "unknown" for a person to settle; none would be resent.
- Rate limiting is per mailbox. There is no per-recipient-domain pacing.
- A domain-wide suppression is not serialised against an in-flight final check (see `outreach-policy.md`).
- Events are written to the outbox but nothing delivers them yet (phase 10).
- A send that fails or is blocked returns the draft for re-approval; there is no one-click retry by design.

## Phase 10 — 9 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 324 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 31 tests passed |
| Migrations up, down to base, up again on a scratch database | Clean through revision 0010 |
| `docker compose exec api python /infra/e2e/phase_c_api.py` on a fresh stack | 18 checks passed: the documented examples over real HTTP with only an API key, synthetic records |

**No external system was contacted.** Webhook delivery was tested against a mocked receiver inside the test process and, for the address guard, against the real guarded network client with a controlled resolver. No delivery has been made to a real external server.

What is covered:

- **Keys.** Shown once; only a hash and a six-character prefix are stored; absent from lists and the audit log. A read-only key gets 403 on fifteen different writes and administrative reads, including sending, approving, key and webhook management, credentials, audit, sign-in details, creating a workspace, inviting an owner and changing settings.
- **Scope ceiling.** Six scopes can be granted: `crm.read`, `crm.write`, `research.run`, `outreach.draft`, `outreach.send`, `reports.read`. Ownership, membership, settings, billing, credentials, approval, audit, deletion, export, research review and call logging are refused for any key. Only an administrator manages keys.
- **A person's decisions stay with people.** A key holding every grantable scope gets 403 on twenty-one actions: approving a message, classifying a recipient, recording consent, lifting a suppression, approving the rules, settling an uncertain send, matching a conversation, lifting a channel restriction, changing a verification, changing a lead's outreach status or qualification, verifying or promoting research, scoring, dismissing, creating or enlarging a research configuration, logging a call or its outcome, exporting, and deleting. (A review found the first version let a key classify recipients and match conversations; two scopes were withdrawn and the remaining decision endpoints now refuse keys.)
- **Attribution.** Activities and audit entries written through a key say so and name the key.
- **Lifecycle.** Malformed, altered, expired and revoked keys get 401. A key never exceeds its creator: demoting the creator removes write access; removing the creator stops the key.
- **Tenant binding.** A tenant named in a header or query is ignored; in a body it is rejected. Another tenant cannot see or revoke the key.
- **Rate limit.** Per key, per minute, with `Retry-After`; another key is unaffected.
- **Idempotency.** The same request with the same key returns the first answer and creates one record. A different body, path or query with that key is `idempotency_conflict`. A refusal is replayed too. The key string is scoped to the API key. A rate-limit answer is not recorded, so the same key works after the wait. A request that ended in a server error, or whose outcome was never recorded, is refused afterwards, not rerun. Six simultaneous identical requests create one record.
- **Webhook addresses.** Fourteen internal, credential-bearing, wrong-scheme and wrong-port addresses are refused; plain HTTP is refused outside development. A public name that resolves to an internal address is looked up and then not connected to.
- **Signing.** The signature verifies with the right secret and fails for a changed body, a wrong secret, a missing or changed timestamp, and a stale delivery.
- **Transactional events.** An event emitted in a transaction that rolls back leaves no event and no delivery. A call outcome reported through the API reaches the endpoints subscribed to it, with one event ID, and not a paused endpoint.
- **Retries and replay.** Seven failures (500, timeout, a redirect to the cloud metadata address, connection refused, 404, 503, 500) follow the documented schedule from the database and end as dead. The redirect is not followed. A replay sends the same event ID; no event or delivery row is duplicated.
- **Secret rotation.** For 24 hours deliveries verify with both secrets, afterwards only the new one. The encryption-key rotation command covers webhook secrets.
- **Isolation.** Another tenant gets 404 on every webhook action; an event in one tenant creates no delivery in another.
- **Examples.** The six documented calls run over HTTP against the local stack; a key cannot approve, cannot raise a research run's limits, a read-only key cannot update, a revoked key stops at once.

**Limitations:**

- The rate limit is a fixed one-minute window per key; a burst across a minute boundary can reach twice the limit.
- Idempotency records are not yet purged (planned with retention in phase 12).
- After a server error, or if the server stops before recording the answer, that key answers `idempotency_in_progress` from then on; the client must read the state and use a new key if the work was not done. This is the documented trade-off for never running a request twice.
- An API key cannot log calls. If an integration should record call outcomes later, they need their own source value so they are never counted as reported by a person.
- The key-rotation script previously covered provider credentials only; it now also covers mailbox tokens and webhook secrets. Only the webhook part has its own test.
- Event coverage is small: email sends, call outcomes, research run finished, and a test event.
- No MCP server is provided; the build pack lists it as optional and later.

## Phase 11 — 9 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 341 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 34 tests passed |

**Stripe has never been contacted.** There is no Stripe account and no approved price (U-08). The provider is a stand-in inside the test process; the real adapter is contract-tested against a mocked transport. Status: `IMPLEMENTED`. It is not `VERIFIED_IN_SANDBOX`.

What is covered:

- **Off by default.** With billing off no limit applies and checkout is refused. A live Stripe key is rejected when settings load.
- **Trial.** A new workspace is on a 14-day trial; the plans offered are labelled as tests with no approved price.
- **Restriction.** After the trial, six kinds of write get 402 for the owner, and the same for an API key; reading, export and billing still work and nothing is deleted.
- **Checkout grants nothing.** Returning to the success address, and a "completed" event with no subscription behind it, leave the workspace restricted. Access comes only when the provider reports an active subscription.
- **Price and plan.** Extra fields such as a price or a tenant are rejected; a non-purchasable plan or a raw price ID is not found; a plan with no price configured cannot be bought; a subscription to an unknown price grants nothing and is flagged.
- **Callbacks.** Unsigned, wrongly signed, altered by one byte and stale callbacks are refused and change nothing.
- **Convergence.** Events delivered late, twice and in reverse order, with payloads claiming older states, always leave the stored state equal to the provider's. A repeated event is recognised. If the provider is unreachable the event is answered 503 and its retry applies the change.
- **Tenant binding.** A callback naming another tenant, a checkout completed for another workspace, and another workspace's subscription attached to this customer all grant nothing here; an unknown customer is ignored.
- **Past due.** Seven days of grace with full access; a second failure notice does not restart the clock; then restricted; payment restores access.
- **Cancellation.** Access continues to the end of the paid period, then the workspace is restricted with its data intact.
- **Seats under concurrency.** Eight simultaneous invitations for two free seats: exactly two succeed.
- **Downgrade.** Below current use: every member keeps access, existing keys keep working, nothing is removed, only adding more is refused, and the page says which limits are exceeded.
- **Workers.** A queued research run pauses without reserving or spending; a queued email is stopped; both name the reason.
- **Monthly limit.** The run over the limit is refused with a message that nothing extra is charged.
- **Access.** Only the owner reaches billing; an administrator and an API key do not; every member can see the standing.
- **Stripe adapter contract.** Customer creation is idempotent per tenant; checkout carries the tenant reference, price and quantity; subscription parsing; 404 and refusals.
- **External Gmail gate.** For each missing prerequisite the availability check, the connect redirect and the callback all refuse; with every prerequisite recorded they still refuse because the flow is not built; the internal pilot is unaffected.

**Limitations:**

- The stand-in models subscription state, not invoices, proration, tax or dunning emails. Real Stripe behaviour is only seen at the sandbox gate.
- Seats are the subscription quantity. Per-seat proration on change is left to Stripe and is untested.
- Monthly counts use calendar months in the workspace time zone, not the billing period.
- Tax, invoicing and account activation are not addressed and no compliance is claimed.
- No platform-provided AI allowance exists; every plan's allowance is zero.

## Phase 12 — 9 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 349 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 37 tests passed |

What is covered:

- **No data is not zero.** In an empty workspace the four rates have value "no data" with a zero denominator, while plain counts are zero. An inverted period and one longer than a year are refused.
- **Totals reconcile with a fixture** inserted with known values: 3 qualified leads of 5; 7 dialler openings but 5 reported calls; connected-call rate 3 of 5 = 60%, never out of the openings; 1 follow-up permission; 3 follow-ups due and 2 done = 66.7%; 3 wins; won amounts as two lines (1500.00 EUR, 300.00 USD) with no total; research cost 4.00 per one qualified research lead. Each figure's record list has exactly as many entries as the figure.
- **Day boundaries.** A call at 22:30 UTC on 31 October belongs to 1 November in Sofia and is counted there; a lead at 21:30 UTC stays on the 31st.
- **Costs.** A reservation whose charge is unknown is listed apart with a note; with costs in two currencies no per-lead figure is produced.
- **Isolation.** Another workspace sees zeros and an empty record list.
- **Daily list.** Empty when nothing is due; a due follow-up and an unassigned qualified lead appear with examples and links; a task due next week and a new deal do not.
- **Retention.** Bounds are enforced (the security log cannot go below a year); a change is audited; a manager cannot change it. The purge blanks the content of a 90-day-old message under a 60-day setting and keeps its subject line and the recent message; it removes old idempotency records and finished jobs; a second run does nothing. Every workspace has the daily job from creation.
- **Erasure.** Refused for a representative, with a wrong name, and without a real reason. Afterwards the company, lead, draft, note, file row and stored file are gone; a do-not-email entry and hashed tombstones remain; neither the tombstones nor the audit log contain the name, domain or list number. Re-importing the same list skips it; so does a renumbered row with the same website and address; mail from its address is not stored while mail from another is; the same business in another workspace is unaffected.
- **Export.** One file per kind of record plus a manifest; a stored provider credential and an API key are not anywhere in the archive; owner only; audited.
- **Workspace deletion.** Owner only, name typed exactly, due in seven days. Run early: nothing is deleted. Cancelled: the job does nothing. When due: the workspace and all its rows are gone, the stored file is gone, its API key stops working, one deletion record remains, and another workspace is untouched.
- **Support access.** Without a grant an outsider cannot open the workspace. Only the owner grants, to a registered non-member, for at most 72 hours with a reason. The grantee can read companies and reports and gets 403 on thirteen other things, including every write, email conversations, the security log, keys, credentials, billing, members and both exports. Starting access is logged once as actor type "support". Conversations become readable only under a grant that includes them. Access ends at expiry and on revocation; it is not a seat. A representative cannot read the security log.

**Limitations:**

- The only report filter is the period.
- The security-log retention setting is stored but the purge does not act on it yet.
- The backup retention window is undefined until hosting exists; re-applying erasures after a restore is manual.
- Erasure removes mail by the business's recorded addresses. Mail from an address never recorded for it is not found.
- "Stale" opportunities are those not updated for 14 days; the threshold is a constant.
- Workspace deletion does not cancel a subscription or stop Gmail watches.

## Phases 11–12 follow-up after review — 9 October 2026

| Command | Result |
|---|---|
| `pytest` (reference workbook present) | 355 passed, 0 skipped |
| `ruff`, `mypy app` | Clean |
| Frontend `typecheck`, `lint`, `test`, `build` | Clean; 37 tests passed |
| Migrations up, down to base, up again on a scratch database | Clean through revision 0013 |

A review of phases 11 and 12 found the following; each was fixed and has a test.

- **A workspace that had not paid could not reduce its own exposure.** The restriction refused every write except billing, including revoking an API key, ending support access, adding an opt-out, cancelling a queued email, erasing a business and deleting the workspace. There is now an explicit allow-list: a restricted owner succeeds on nine such actions (and an API key on the two it may call), while creating anything still gets 402. A path that merely contains the word "billing" is no longer a way round.
- **Support access could read communications by side doors** (queued sends, the attention list, report records, activities, suppressions, mailbox status). Support access is now an allow-list. A test calls every GET route in the API as a grantee: exactly the listed routes answer, and adding communications opens only the communications list.
- **Erasure ignored the send ordering rule and left copies.** It now takes the recipient lock first, refuses while an email to the business is being sent or its outcome is unknown, cancels a waiting one, and also removes the business's imported rows, the original uploaded files that contained it, and matching research candidates. Research runs no longer store an erased business as a candidate. A check that previously asserted nothing now queries assessments, observations and scores by the erased lead. Matching by email address alone (new number, new name, no website) is tested.
- **A report figure did not match what the application records.** A follow-up created by a call outcome is a task of kind "call", so "follow-ups due" would have shown nothing in real use. It now counts the task the call created. A new test builds its data only through the API (call, outcome with follow-up, meeting, deal moved through proposal to won) so the figures cannot drift from the screens again.
- **Three named figures were missing.** Positive replies, booked meetings and proposals now exist, each from something a person records; enthusiastic wording in a reply changes nothing until a person marks it.
- **Withdrawing a mailbox only forgot the token locally.** Disconnecting and workspace deletion now stop Gmail notifications and revoke the authorisation at Google; if that fails, the audit entry says what is left to do by hand. Contract-tested against a mocked transport only.
- Also: a payment refusal or a plan-limit refusal is no longer stored against an idempotency key; the security-log retention setting now has effect, through a function that cannot go below a year or outside the current workspace; a workspace with a running subscription cannot be scheduled for deletion; the erasure hashes have their own key; the application's database role can no longer read billing events of all tenants; granting support access no longer reveals whether an address has an account.

Not covered by a test: that a research run skips an erased business (the check is in place and uses the same function the import and mailbox tests exercise).

## Phase 13 — 9 October 2026

**Nothing was deployed.** There is no hosting target (U-07). This phase is prepared, not complete.

| Check | Result |
|---|---|
| `pytest` (reference workbook present) | 356 passed, 0 skipped |
| `ruff`, `mypy app`; OpenAPI file current | Clean |
| `docker compose -f infra/production/compose.yaml … config` with every profile | Validates. Without image references it refuses to render |
| Only the proxy publishes ports in the rendered configuration | Confirmed (80 and 443); database and Redis are on an internal network |
| Settings loaded from `infra/production/env.example` in a staging environment | Refused: "session_secret still contains a placeholder value" |
| `deploy.sh` with images not pinned by digest | Refused |
| `remote.sh` and `smoke-remote.sh` with no host configured | Do nothing and say so |
| Shell scripts parse (`sh -n`); workflow and Compose YAML parse | Yes. One quoting error in `remote.sh` was found this way and fixed |
| `infra/checks/upgrade-from-previous.sh` | Passed: schema at revision 0008 with a workspace and a company, upgraded to 0014; data kept, retention job created for the existing workspace, plans seeded |
| Migrations up, down to base, up | Clean through 0013 (run before 0014 was added; 0014 was applied by the upgrade check and the test suite, its downgrade has not been run) |
| Encrypted backup on the local stack | Created (about 416 KB); not readable by `pg_restore` without the passphrase |
| `restore-check.sh` on the encrypted backup | Passed: 70 tables, 216 rows, 83 policies, row-level security intact |
| Wrong passphrase; file altered by one byte | Both refused (decryption failure; checksum mismatch) |
| Backups older than the retention window | Removed by the next run |
| `/ops/metrics` | Closed without a token and to a wrong one; with it, totals across two workspaces and nothing identifying either |
| Development-stack smoke test (`infra/smoke.sh`) and API examples | Passed on a fresh stack |

**Not done, and why:**

- **Production images were not built at the current commit.** `docker build` could not resolve base-image metadata from Docker Hub and GHCR from this machine (timeouts). The production stages of both Dockerfiles are unchanged since the `images` job built them on GitHub for pull request 2. `infra/checks/image-checks.sh` has therefore **never been run**; it is written and parses.
- **The production stack was not started**, for the same reason. It is validated as configuration only.
- **The release workflow has never run.** It reuses the CI workflow, builds once, pushes under the commit SHA and deploys digests; the deploy and smoke jobs skip themselves without a host. The `production` environment's required-reviewer rule is a repository setting that does not exist yet.
- **Rollback was not rehearsed.** The procedure and its limits are written down.
- **Three images are pinned by tag, not digest** (uv, nginx-unprivileged, caddy); their digests could not be fetched in this session. Earlier status text saying all base images were pinned by digest was wrong and is corrected.
- No error-tracking service is connected. No off-host backup destination or schedule exists.
- The restore check found a stale local database (an edited migration applied earlier), not a product fault; it passed on a fresh stack. It now names the table when it fails.
