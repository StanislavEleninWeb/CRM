# Release gates

Each gate is `PASS`, `FAIL`, `BLOCKED` or `NOT_STARTED`. Evidence is in `test-evidence.md`. A gate passes only on verified behaviour, never on the existence of files.

## Phase A — call-first pilot (phases 00–05)

| Gate | Status |
|---|---|
| Two-tenant isolation verified as the runtime database role | PASS for identity tables (phase 02); CRM tables follow in phase 03 |
| Reference workbook import: all fixture assertions | NOT_STARTED |
| Deterministic scoring reproduces tiers and boundaries | NOT_STARTED |
| Import → score → queue → call outcome → follow-up end to end | NOT_STARTED |
| Date-only stability and mobile layout verified | NOT_STARTED |
| Local backup and restore exercised | NOT_STARTED |
| Hosted pilot security and deployment checks | BLOCKED — no hosting target (U-07) |

## Phase B — research and internal Gmail pilot (phases 06–09)

| Gate | Status |
|---|---|
| Budgets atomic under concurrency | NOT_STARTED |
| Synthetic research run end to end | NOT_STARTED |
| Live research run | BLOCKED — provider, terms and budget (U-03, U-04) |
| Gmail sync contract verified against a fake provider | NOT_STARTED |
| Live internal Gmail connection | BLOCKED — mailbox and project ownership (U-02) |
| Email eligibility enforced at send time | NOT_STARTED |
| Live unsolicited email | BLOCKED — policy approval (U-06) |
| Due-row dispatcher: single claim, crash recovery, no blind resend | NOT_STARTED |

## Commercial core (phases 10–14)

| Gate | Status |
|---|---|
| Scoped API keys and idempotency | NOT_STARTED |
| Signed webhooks with replay safety | NOT_STARTED |
| Billing lifecycle in Stripe test mode | BLOCKED — account and plans (U-08) |
| Entitlements enforced on the server | NOT_STARTED |
| Retention, export, deletion, support access | NOT_STARTED |
| Images, pipeline, restore, rollback | NOT_STARTED |
| Staging deployment | BLOCKED — no hosting target (U-07) |

## External Gmail launch (CRM-114)

| Gate | Status |
|---|---|
| Scope list and data-flow documentation | NOT_STARTED |
| Google verification for sensitive and restricted scopes | BLOCKED (U-09) |
| Annual security assessment: quote, owner, validity date | BLOCKED (U-09) |
| All onboarding paths reject external Gmail while the gate is closed | NOT_STARTED |

The commercial core may launch with external Gmail disabled, provided that is clearly disclosed.
