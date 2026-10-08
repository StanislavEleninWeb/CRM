# Task backlog

Stable IDs from build-pack revision 2. States: `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`. A task is `DONE` only when its acceptance was verified; evidence is in `test-evidence.md`. Priority: P0 foundational or security blocker, P1 pilot, P2 commercial, P3 optional.

## Phase 00 — scope, repository, decisions (P0)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-001 | Repository audit | Workspace, branch and existing work inspected; nothing overwritten | DONE |
| CRM-002 | Scope and decisions | Every requirement has an owner task; choices recorded as confirmed, default or unresolved | DONE |
| CRM-003 | Backlog and checkpoints | Stable IDs copied; status documents exist; placeholders-only environment example | DONE |

## Phase 01 — executable foundation (P0, depends on 00)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-010 | Scaffold: FastAPI app, session layer, Alembic, error envelope, correlation IDs, pagination, settings validation, log redaction | Clean checkout starts with documented commands | DONE |
| CRM-011 | Local containers: API, worker, scheduler, frontend, PostgreSQL, Redis, object storage | Readiness reflects database availability; worker runs a synthetic job | DONE |
| CRM-012 | Migrations and CI | Migrations apply from empty; CI commands run locally | DONE |
| CRM-013 | Frontend shell | Responsive shell calls the real API; loading, empty and error states | DONE |

## Phase 02 — identity, tenancy, roles, isolation (P0, depends on 01)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-020 | Identity and sessions (OIDC, HttpOnly cookies, CSRF, revocation) | Issuer, audience, signature, state, nonce and PKCE validated | DONE |
| CRM-021 | Tenants, memberships, invitations | Expired or reused invitations fail; removed members lose access | DONE |
| CRM-022 | Roles and permission matrix | Role escalation fails; read-only cannot mutate; last owner cannot be removed | DONE |
| CRM-023 | Row-level security isolation | Two tenants cannot cross-read, write or link; missing context fails closed; pooled connections do not leak; tests run as the runtime role | DONE |

## Phase 03 — core CRM and history (P1, depends on 02)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-030 | Companies, contacts, contact channels, leads, deals, restrictions | Cross-tenant links fail in database and API; emergency numbers excluded from dialling | DONE |
| CRM-031 | Pipelines, stages, tasks, next actions | Lead converts to opportunity; stage history kept | DONE |
| CRM-032 | Activity timeline and audit events | History survives edits and merges | DONE |
| CRM-033 | Search, filters, saved views, files | Pagination and filtering work at volume; file URLs cannot cross tenants | DONE |

## Phase 04 — research schema, scoring, import and export (P1, depends on 03)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-040 | Research schema: assessments, observations, evidence, hypotheses, recommendations, tags | Provenance and raw values retained | TODO |
| CRM-041 | Deterministic rubric | Server-computed totals and tiers; overrides with reasons; versioned history | TODO |
| CRM-042 | CSV/XLSX importer | All fixture assertions in `approved-scope.md`; re-import creates no duplicates | TODO |
| CRM-043 | Reference export | Round trip preserves IDs and fields; formula injection neutralised | TODO |

## Phase 05 — prospect review, shortlist, calls (P1, depends on 04)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-050 | Prospect workspace: table, mobile cards, filters | Ranking and filters reflect edits | TODO |
| CRM-051 | Verification queue | Stale or uncertain findings route to verification | TODO |
| CRM-052 | Daily shortlist snapshots | Dated in tenant time zone; fillers labelled; never padded | TODO |
| CRM-053 | Mobile call outcomes and follow-ups | Dialer launch is separate from a reported call; follow-ups persist | TODO |

## Phase 06 — provider connections and usage (P0, depends on 02–05)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-060 | Secure provider connections | Secrets encrypted with versioned keys; rotation works; never returned | TODO |
| CRM-061 | Usage ledger | Estimated and verified costs distinguished | TODO |
| CRM-062 | Atomic budgets | Concurrent jobs cannot oversubscribe; retries do not double-reserve | TODO |
| CRM-063 | Provider health | Revoked credentials fail visibly; reconnect keeps history | TODO |

## Phase 07 — evidence-based AI research (P1, depends on 06)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-070 | Research orchestrator | Synthetic run links evidence, scores, drafts, summary; no padding | TODO |
| CRM-071 | Licensed discovery adapter | Contract-tested; live status reported separately | TODO |
| CRM-072 | Isolated inspection | SSRF, redirect, IPv6 and rebinding cases fail safely | TODO |
| CRM-073 | AI score proposals | Schema violations go to review; prompt injection has no effect | TODO |
| CRM-074 | Run controls | Pause, cancel, resume; refresh preserves outreach | TODO |
| CRM-075 | Source and retention enforcement | Field-policy expiry and export blocking tested | TODO |

## Phase 08 — internal Gmail and eligibility (P1, depends on 05, 06)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-080 | Internal Gmail OAuth | State validated; external organisations rejected | TODO |
| CRM-081 | Manual draft, send request, thread | Sends stay dry-run until phase 09 | TODO |
| CRM-082 | History sync | Cursor gaps, 404, dropped and reordered notifications handled | TODO |
| CRM-083 | Watch renewal and recovery | Renewal failure alerts before expiry | TODO |
| CRM-084 | Bulgarian eligibility policy | Unknown classification and stale register block unsolicited sends | TODO |

## Phase 09 — reliable manual sends (P0, depends on 05, 08)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-094 | Database due dispatcher | Two pollers claim each row once; crash recovery; no lock across network calls | TODO |
| CRM-091 | Manual-send approvals | Edits invalidate approval | TODO |
| CRM-092 | Suppression | Reply or opt-out before dispatch blocks | TODO |
| CRM-093 | Reliable sends | Ambiguous outcomes reconciled, never blindly resent | TODO |

## Phase 10 — public API, webhooks, Hermes (P2, depends on 07, 09)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-100 | Scoped keys | Read-only key cannot mutate; revoked and expired keys fail | TODO |
| CRM-101 | Public API | Idempotency conflicts rejected | TODO |
| CRM-102 | Outbox and webhooks | Signatures reject tampering; replay does not duplicate | TODO |
| CRM-103 | Hermes examples | Examples run against local with synthetic records | TODO |

## Phase 11 — subscriptions and entitlements (P2, depends on 06, 10)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-110 | Billing sandbox | Stripe test mode when credentials exist | TODO |
| CRM-111 | Entitlements | Enforced on API and workers | TODO |
| CRM-112 | Lifecycle and quota enforcement | Out-of-order events converge; downgrade keeps data | TODO |
| CRM-113 | Billing portal | Owner access | TODO |
| CRM-114 | External Gmail verification and cost gate | Onboarding rejected while gate closed | TODO |

## Phase 12 — reporting and operational controls (P2, depends on 09–11)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-120 | Funnel metrics | Totals reconcile with fixtures; honest zero denominators | TODO |
| CRM-121 | Daily workspace | Replies, follow-ups, stale deals, failures | TODO |
| CRM-122 | Retention, export, deletion | Deleted records do not reappear | TODO |
| CRM-123 | Support access | Expires and is logged | TODO |

## Phase 13 — staging and deployment pipeline (P0, depends on 12)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-130 | Immutable images | Images build; no secrets inside | TODO |
| CRM-131 | GitHub Actions release | Failed health or migration blocks promotion | TODO |
| CRM-132 | Staging and runbooks | Smoke checks pass locally or on staging | TODO |
| CRM-133 | Backup recovery | Isolated restore recovers data | TODO |

## Phase 14 — release evidence and handoff (P0, depends on 13)

| ID | Deliverable | Acceptance | Status |
|---|---|---|---|
| CRM-140 | End-to-end evidence | Cross-stack scenarios with two tenants as runtime role | TODO |
| CRM-141 | Adversarial isolation | API, workers, pools, files, exports, webhooks, caches | TODO |
| CRM-142 | Failure rehearsals | Duplicate delivery, notification loss, races | TODO |
| CRM-143 | Pilot handoff | Release checklist with PASS, FAIL or BLOCKED per gate | TODO |

## Phase 15 — deferred (P3, not run implicitly)

| ID | Deliverable | Status |
|---|---|---|
| CRM-090 | Email sequences | TODO (deferred) |
| CRM-150 | Zadarma adapter | TODO (deferred) |
| CRM-151 | ElevenLabs calls | TODO (deferred) |
| CRM-152 | Calendar and proposals | TODO (deferred) |
| CRM-153 | MCP server | TODO (deferred) |
| CRM-154 | Microsoft mailbox | TODO (deferred) |
