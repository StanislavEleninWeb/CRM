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
| D-09 | Hosting: one VPS, `161.97.89.47`, address `crm.seweb.co`, certificate notices to `management@seweb.co`. One environment, production; no separate staging | Owner, 9 Oct 2026 |
| D-10 | Deployment through GitHub Actions over SSH; access supplied as environment variables and a secret | Owner, 9 Oct 2026 |
| D-11 | Files and off-host backups on AWS S3 | Owner, 9 Oct 2026 |
| D-12 | Sign-in through AWS Cognito | Owner, 9 Oct 2026 |

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

## Call queue rules (phase 05)

Decided while testing the queue on real data; shown to users under "How the queue moves on".

| Situation | Behaviour |
|---|---|
| A follow-up is due | First in the queue, whatever happened before |
| A follow-up is booked for a later day | Waits until that day |
| A call outcome was reported today | Not offered again today |
| Connected, nothing booked | Leaves the cold-call queue; work continues through a follow-up or an opportunity |
| No answer, busy, voicemail | Returns on a later day |
| Wrong number | Stays in the queue if another number may be called |
| Dialler opened, no outcome reported | Stays in the queue: nothing is known to have happened |

## Billing policy (phase 11)

Working defaults, changeable by the owner. None of this is a price.

| Situation | Behaviour |
|---|---|
| New workspace | 14-day trial on the trial plan, dated from creation |
| Trial ends with no subscription | Restricted |
| Payment overdue | 7 days of grace with full access, then restricted |
| Cancellation | Access until the end of the paid period, then restricted |
| Restricted | Read, export and billing only. Nothing is created, sent or deleted |
| Running research when restricted | Pauses before its next paid step; cost already incurred is settled; resumable after payment |
| Queued email when restricted | Stopped before sending; one already with the mailbox provider is unaffected |
| Downgrade below current use | Members, keys and data are kept; only adding more is refused |
| Over a limit | The action is refused. There is no paid overage |
| Platform-provided AI | Not offered: the allowance is zero on every plan. Tenants bring their own key |
| Who manages billing | The owner only |
| Internal pilot | `BILLING_MODE=off`: nothing is limited or charged |

## Unresolved

| # | Choice | Blocks | Needed by |
|---|---|---|---|
| U-01 | ~~Production identity provider~~ Decided: AWS Cognito (D-12). Open: the user pool itself, and a first real sign-in | Production sign-in | Hosted pilot |
| U-02 | Outreach mailbox address and confirmation that the Google Cloud project is owned by the SEWEB organization | Live Gmail connection | Phase 08 live gate |
| U-03 | AI model provider, access mode, and monthly budget | Live research runs | Phase 07 live gate |
| U-04 | Google Places API account billing region and applicable terms (see below) | Live discovery ingestion | Phase 07 live gate |
| U-05 | Scraping provider and its source terms | Later research phase | After phase 07 |
| U-06 | Bulgarian unsolicited-email policy: current statute text, sole-trader treatment, register access and freshness | Live unsolicited email | Phase 08 and 09 live gates |
| U-07 | ~~Hosting target and URL~~ Decided (D-09 to D-11). Open: DNS record, buckets and keys, server preparation, GitHub environment, how the server's existing Caddy is run | First deployment | Phase 13 |
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
