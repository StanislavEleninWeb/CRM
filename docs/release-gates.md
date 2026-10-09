# Release gates

**For the current overall picture and the capability matrix, read `release-checklist.md`.** This file keeps the per-phase gate history.

Each gate is `PASS`, `FAIL`, `BLOCKED` or `NOT_STARTED`. Evidence is in `test-evidence.md`. A gate passes only on verified behaviour, never on the existence of files.

## Phase A — call-first pilot (phases 00–05)

| Gate | Status |
|---|---|
| Two-tenant isolation verified as the runtime database role | PASS for identity and CRM tables (phases 02–03); re-verified for each later phase by the schema guard test |
| Reference workbook import: all fixture assertions | PASS (local, private fixture; skipped in public CI) |
| Deterministic scoring reproduces tiers and boundaries | PASS |
| Import → score → queue → call outcome → follow-up end to end | PASS (`infra/e2e/phase_a.py`, real worker, 47 checks) |
| Date-only stability and mobile layout verified | PASS (four time zones; 375 px browser check) |
| Local backup and restore exercised | PASS (`docs/runbooks/backup-restore.md`) |
| Hosted pilot security and deployment checks | BLOCKED — no hosting target (U-07) |

## Phase B — research and internal Gmail pilot (phases 06–09)

| Gate | Status |
|---|---|
| Budgets atomic under concurrency | PASS (20 threads on separate connections) |
| Synthetic research run end to end | PASS (tests, and `infra/e2e/phase_b_research.py` through the real scheduler) |
| Live research run | BLOCKED — provider, terms and budget (U-03, U-04) |
| Gmail sync contract verified against a fake provider | PASS (fake mailbox and mocked HTTP transport) |
| Live internal Gmail connection | BLOCKED — mailbox and project ownership (U-02) |
| Email eligibility enforced at approval, at the send request and immediately before dispatch | PASS (local) |
| Live unsolicited email | BLOCKED — policy approval (U-06) |
| Due-row dispatcher: single claim, crash recovery, no blind resend | PASS (local, PostgreSQL, eight concurrent workers, fake mailbox) |
| No database lock or open transaction during the provider call | PASS (local) |

### Phase B gate, by concern

| Concern | Status | What it rests on |
|---|---|---|
| Software: research, mailbox sync, eligibility, dispatch | VERIFIED_LOCALLY | Automated tests against PostgreSQL with local stand-ins |
| Provider: Gmail | IMPLEMENTED, not connected | No Google account has been used. Needs U-02 |
| Provider: Google Places, AI model | IMPLEMENTED (Places contract only), not connected | Needs U-03, U-04 |
| Policy: Bulgarian outreach rules | Draft, not approved | Needs qualified review and owner approval (U-06) |
| Live test: one approved email sent and its reply tracked | NOT RUN — BLOCKED | Needs the three rows above. `EMAIL_DISPATCH` is `off` |

## Commercial core (phases 10–14)

| Gate | Status |
|---|---|
| Scoped API keys and idempotency | PASS (local) |
| Signed webhooks with replay safety | PASS (local, mocked receiver). Delivery to a real external receiver has not been exercised |
| Billing lifecycle against a stand-in provider (duplicates, reordering, grace, cancellation, downgrade) | PASS (local) |
| Billing lifecycle in Stripe test mode | BLOCKED — account and plans (U-08). Status: IMPLEMENTED, not VERIFIED_IN_SANDBOX |
| Approved plans and prices; tax, invoicing and account activation | BLOCKED — owner decisions (U-08). Not claimed |
| Entitlements enforced on the server, for API keys and in workers | PASS (local) |
| Report totals reconcile with a fixture; zero denominators shown as no data; time-zone day boundaries | PASS (local) |
| Erased records do not return through import, research or the mailbox | PASS (local) |
| Workspace export, and deletion after a cancellable waiting period | PASS (local) |
| Support access: owner-granted, read-only, logged, expiring | PASS (local) |
| Backup retention window defined; erasure re-applied after a restore | NOT_STARTED — needs the hosting target (U-07); documented as a manual procedure |
| Production images build and pass the image checks | NOT VERIFIED at the current commit — registry lookups timed out locally; the unchanged production stages built on GitHub for PR 2 |
| Production configuration validates; placeholders and unpinned images are refused | PASS (local) |
| Release pipeline run end to end | NOT RUN — never executed; needs environments and a host |
| Clean migration, and upgrade from the first published revision with data | PASS (local) |
| Encrypted backup restores into an isolated database | PASS (local) |
| Rollback rehearsed | NOT RUN — procedure and a rollback mode in the deploy script exist; never executed on a host |
| Running services cannot use the schema owner's credentials | PASS for the rendered production configuration (only the migration job receives them) |
| Staging deployment | BLOCKED — no hosting target (U-07) |

## External Gmail launch (CRM-114)

| Gate | Status |
|---|---|
| Scope list and data-flow documentation | DONE (`docs/external-gmail-gate.md`) |
| Google verification for sensitive and restricted scopes | BLOCKED (U-09) |
| Annual security assessment: quote, owner, validity date | BLOCKED (U-09) |
| All onboarding paths reject external Gmail while the gate is closed | PASS: availability, connect and callback, for each missing prerequisite and with all recorded. Billing adds no onboarding path to Gmail |

The commercial core may launch with external Gmail disabled, provided that is clearly disclosed.
