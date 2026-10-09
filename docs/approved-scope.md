# Approved scope

Derived from discovery proposal v2 and build-pack revision 2 (8 October 2026). Where they differ, the build pack wins. This is a summary written for this repository; the source documents are not committed.

## Product

An API-first, multi-tenant B2B sales CRM. SEWEB is the first tenant and the initial configuration, not hard-coded logic. Main loop: discover companies → verify evidence and contacts → qualify → prepare outreach → communicate → follow up → manage opportunities → learn what converts.

## Stack (approved)

Python, FastAPI, PostgreSQL, SQLAlchemy, Alembic; TypeScript, React, Vite; Celery and Redis; S3-compatible file storage; Docker; GitHub Actions. Modular monolith. Shared database with `tenant_id` and PostgreSQL row-level security.

## Releases

| Release | Phases | Contents |
|---|---|---|
| A — call-first pilot | 00–05 | Core CRM, deterministic scoring, XLSX/CSV import and export, call-first daily queue, manual call outcomes |
| B — research and Gmail pilot | 06–09 | Provider connections and budgets, AI research with score proposals, internal Gmail manual send and reply tracking, reliable due-row dispatch |
| C — commercial core | 10–14 | Public API keys, webhooks, billing and entitlements, reporting, deployment pipeline, release evidence |
| Later | 15 | Email sequences, Zadarma, ElevenLabs, calendar and proposals, MCP server, Microsoft mailbox |

Phase A acceptance must not wait for Gmail, an AI key, billing, or sequences. External Gmail stays disabled until its own gate passes (CRM-114).

## Roles

Owner, administrator, sales manager, representative, read-only, and independently scoped integration principal. Tenant-wide sales visibility initially. Platform operators have no default access to tenant communications; support access is explicit, time-limited, and audited.

## Reference workbook mapping

The workbook has three sheets. Only `All qualified leads` creates leads. The shortlist sheet is a view over the same Lead IDs. `Research summary` is retained as source context.

| Workbook columns | CRM representation |
|---|---|
| Lead ID, Business name | Internal UUID plus tenant-scoped external lead ID; company identity |
| Industry / business type, Industry group | Specific business type; normalized industry group |
| City, Country | Geography filters |
| Website URL | Official website with structured missing state |
| Google Business Profile / Maps URL | Original listing URL plus typed identifier (`place_id`, `cid`, `unknown`) |
| Public business phone, Public business email, Contact page URL, Other public business contact channel | Typed contact channels with purpose, source, date, verification state, raw cell retained |
| Website status | Base enum plus flags (availability, transport, asset/page issues), raw text retained |
| Specific observed issue or opportunity, Evidence / relevant page URL | Observations with source evidence |
| Opportunity hypothesis requiring confirmation | Hypotheses, stored separately from observations |
| Recommended SEWEB service, Service category | Tenant service catalog and recommendations |
| Why suitable, Suggested business benefit | Fit explanation; proposed benefit (not a measured outcome) |
| Personalized outreach opening, Suggested discovery question | Editable drafts tied to evidence |
| Recommended contact channel | Preferred channel, ordered fallbacks, instruction text |
| Priority score, tier, five score components | Versioned rubric, component values, server-computed total and tier, history |
| Evidence confidence, Date checked | Confidence (independent of score); date-only assessment date |
| Outreach status, Notes | Outreach workflow state; raw notes |
| Industry group, Service category, Observed opportunity tags | Normalized classification; tags (including `Other`) |

All 35 columns are preserved through mapped fields and a retained raw row.

## Default scoring rubric

| Component | Maximum |
|---|---:|
| Evidence strength | 30 |
| Relevance to tenant services | 25 |
| Plausible business value | 20 |
| Reachability | 15 |
| Signs of current business activity | 10 |

Total is computed on the server. Tier A is 80–100, B is 60–79, C is below 60. Unassessed is not zero. Confidence is independent of score. Overrides need a reason and are kept in history. The rubric is versioned.

## Fixture assertions (phase 04 and 14)

These hold for the supplied workbook and are test assertions, not production constants. All were checked against the file on 8 October 2026.

- 94 leads, 35 columns, unique Lead IDs; 25 shortlist rows all resolve to existing leads.
- Tiers 21 A / 62 B / 11 C. Confidence 31 high / 58 medium / 5 low.
- Every total equals its component sum; no component exceeds its cap; scores at 59, 60, 79 and 80 all occur.
- Shortlist is the top 25 by score: 21 A plus 4 B, minimum score 77.
- 17 phone cells contain a semicolon; labels `delivery` and `emergency` occur.
- 14 raw website-status values; 4 raw contact-channel values; 66 phone-first recommendations (65 `Phone` plus `Phone (ask for the manager)`).
- 176 literal `Not found` values: website 30, phone 1, email 58, contact page 46, other channel 41.
- 79 `place_id` listing links and 15 `cid` links.
- 36 emails, 19 on free-mail domains (10 abv.bg, 8 gmail.com, 1 mail.bg).
- 30 leads with neither website nor email.
- `Date checked` is a date-only value (7 October 2026) and must not shift across time zones.

## Requirement-to-task matrix

Discovery acceptance gates (§11) and build-pack phase gates, each with an owning task.

| Requirement | Owner task | Phase |
|---|---|---|
| Two tenants cannot access each other's data through UI, API, jobs, exports, files | CRM-023, CRM-033, CRM-141 | 02, 03, 14 |
| Missing tenant context fails closed; runtime role has no BYPASSRLS | CRM-023 | 02 |
| Imports are reviewable (mapping preview, validation, duplicate review) | CRM-042 | 04 |
| Activities remain linked after merging | CRM-030, CRM-032 | 03 |
| Workbook import yields 94 leads, shortlist rows link to them, no duplicates on re-import | CRM-042 | 04 |
| Default tiers reproduce 21/62/11; totals match components | CRM-041 | 04 |
| Export and re-import round trip preserves IDs and reference fields | CRM-043 | 04 |
| High-priority uncertain claims go to verification; hypotheses are never stated as facts | CRM-051, CRM-073 | 05, 07 |
| Daily shortlist in tenant time zone, Tier B fillers labelled, never padded | CRM-052 | 05 |
| Dialer launch is not recorded as a completed call; outcomes logged quickly | CRM-053 | 05 |
| Channel do-not-contact flags honoured; emergency numbers excluded from sales dialling | CRM-030, CRM-053 | 03, 05 |
| Tenant budgets cannot be exceeded by concurrent jobs | CRM-062 | 06 |
| Provider secrets encrypted, rotated, never returned or logged | CRM-060 | 06 |
| Leads include evidence and dates; fabricated contacts are rejected | CRM-070, CRM-073 | 07 |
| A research run may return fewer leads than requested, with reasons | CRM-070, CRM-074 | 07 |
| Refresh preserves prior outreach and original evidence | CRM-074 | 07 |
| Source storage, expiry and export rights enforced per field | CRM-075 | 07 |
| SSRF, redirect and prompt-injection cases fail safely | CRM-072, CRM-073 | 07 |
| Replies link to the right conversation; cursor gaps and dropped notifications recovered | CRM-082, CRM-083 | 08 |
| Bulgarian email eligibility enforced at send time; unknown facts trigger review | CRM-084 | 08 |
| Suppression and replies block future sends even when already queued | CRM-092, CRM-094 | 09 |
| Retry and reconciliation do not silently duplicate sends | CRM-093, CRM-094 | 09 |
| Scoped keys cannot exceed permissions; replayed webhooks do not duplicate actions | CRM-100, CRM-102 | 10 |
| API examples run against local or staging | CRM-103 | 10 |
| Payment state controls entitlements on the server | CRM-111, CRM-112 | 11 |
| External Gmail onboarding rejected while its gate is closed | CRM-114 | 11 |
| Report totals reconcile; denominators and cohorts defined | CRM-120 | 12 |
| Retention, export, deletion; time-limited audited support access | CRM-122, CRM-123 | 12 |
| Backup restore demonstrated; monitoring catches failed workers and sync | CRM-132, CRM-133 | 13 |
| Expired credentials can be reconnected without losing history | CRM-063, CRM-083 | 06, 08 |
| End-to-end evidence and capability release matrix | CRM-140, CRM-143 | 14 |

## Excluded from the core build

Native mobile apps, full helpdesk, accounting, automatic social posting, unrestricted autonomous outreach, predictive scoring, a visual workflow builder, support for every provider, and everything in phase 15 unless explicitly requested.
