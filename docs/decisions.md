# Decisions

Status values: **Confirmed** (the owner said so), **Default** (working default from the build pack, changeable), **Unresolved** (needs a choice before the dependent phase goes live).

## Confirmed

| # | Decision | Source |
|---|---|---|
| D-01 | Stack: FastAPI, PostgreSQL, SQLAlchemy, Alembic, React, Vite, TypeScript, Celery, Redis, Docker, GitHub Actions; modular monolith | Build pack |
| D-02 | Shared database with `tenant_id` and row-level security; non-owner runtime role without `BYPASSRLS` | Build pack |
| D-03 | Release order: call-first pilot (A), research and manual Gmail (B), commercial core (C); sequences deferred to phase 15 | Build pack rev 2 |
| D-04 | Deterministic scoring rubric belongs to phase A and does not depend on an AI provider | Build pack rev 2 |
| D-05 | First mailbox provider is Google Workspace; `seweb.co` is on Google Workspace | Owner, 8 Oct 2026 |
| D-06 | Research uses the Google Places API first and a scraping provider later | Owner, 8 Oct 2026 |
| D-07 | Schedules are PostgreSQL due rows; no multi-day Celery ETA tasks | Build pack rev 2 |
| D-08 | Repository `StanislavEleninWeb/CRM` is public; real prospect data stays in ignored paths | Repository state |

## Defaults

| # | Default | Reason |
|---|---|---|
| F-01 | SEWEB internal pilot first; responsive web app; no native apps | Build pack |
| F-02 | Mobile dialer (`tel:`) with manual outcomes; no provider-confirmed calls in core | Build pack |
| F-03 | Tenant currency EUR, time zone Europe/Sofia; both are tenant settings | Bulgaria adopted the euro on 1 January 2026 |
| F-04 | Bulgarian-market drafts default to Bulgarian, English optional | Build pack |
| F-05 | Local development identity: an isolated development OIDC provider in Compose | Build pack allows this as a test default only |
| F-06 | Reference-workbook tests use the `reference_fixture` pytest marker and report *skipped: reference fixture absent* in public CI | The fixture cannot be committed to a public repository |
| F-07 | Local commits per phase on a `build/*` branch; no push or pull request until asked | Pushing to a public remote is outward-facing |
| F-08 | Backend runs on a pinned Python 3.13 image; the host's Python 3.14 is not used for the app | Library support for 3.14 is not assumed |
| F-09 | Shortlist size 25, due-row poll interval 30 seconds, mailbox reconciliation every 15 minutes; all configurable | Build pack |

## Unresolved

| # | Choice | Blocks | Needed by |
|---|---|---|---|
| U-01 | Production identity (OIDC) provider | Production sign-in; MFA verification | Hosted pilot |
| U-02 | Outreach mailbox address and confirmation that the Google Cloud project is owned by the SEWEB organization | Live Gmail connection | Phase 08 live gate |
| U-03 | AI model provider, access mode, and monthly budget | Live research runs | Phase 07 live gate |
| U-04 | Google Places API account billing region and applicable terms (see below) | Live discovery ingestion | Phase 07 live gate |
| U-05 | Scraping provider and its source terms | Later research phase | After phase 07 |
| U-06 | Bulgarian unsolicited-email policy: current statute text, sole-trader treatment, register access and freshness | Live unsolicited email | Phase 08 and 09 live gates |
| U-07 | Hosting target and URL | Staging and production deployment | Phase 13 |
| U-08 | Stripe account, plan definitions and prices | Billing sandbox and live billing | Phase 11 |
| U-09 | External Gmail verification and security-assessment quote | External tenants connecting Gmail | CRM-114 |
| U-10 | Whether the discovery proposal and build pack may be committed to this public repository | Nothing; they are summarised in `approved-scope.md` | Owner preference |

## Corrections to the earlier review

`docs/discovery-review.md` was written before the build pack. Two points changed:

- **Listing identifiers.** The review said all 94 Maps URLs are `place_id` links. The workbook has 79 `place_id` links and 15 `cid` links. A `cid` is stored as a separately typed identifier and is not assumed to share the `place_id` storage exemption.
- **Places API terms.** The review cited the global Google Maps Platform terms (§3.2.3 and §14.3). An account billed in Bulgaria falls under the EEA terms, which were not fetched. The storage rule is therefore **unverified for an EEA account** and is owned by CRM-075. Refetching review counts at scoring time is not assumed to be permitted either.

## Notes on specific choices

- **Gmail scopes.** `gmail.send` is a sensitive scope and cannot read replies. Reply tracking needs a restricted read scope. An Internal OAuth app in SEWEB's own Workspace is exempt from verification; that exemption must never be used to connect another tenant.
- **Free-mail addresses.** A free-mail domain is not a legal classification. Recipient legal form is recorded from evidence or left `unknown`, which routes to review.
- **Old fines.** The 2019 English consolidation of the Electronic Commerce Act quotes fines in BGN. No fine amounts are encoded in the product.
