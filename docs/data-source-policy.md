# Data source policy

Field-level provenance and permitted use. Every stored research field carries a source type, a source reference, an obtained date, and, where the source requires it, an expiry and an export restriction. This document is the human-readable policy; the enforced version lives in the `source_policies` table (CRM-075).

## Source types

| Source type | Meaning | Storage | Export | Status |
|---|---|---|---|---|
| `user_import` | Uploaded by a tenant user (for example the reference workbook) | Permanent, tenant-owned | Allowed | Applies now |
| `official_website` | Read from the business's own website | Permanent with URL and date | Allowed | Applies from phase 07 |
| `manual_entry` | Typed by a user, with optional source note | Permanent | Allowed | Applies now |
| `google_places` | Returned by the Google Places API | **Unverified for an EEA account** — see below | Blocked until verified | Blocked (U-04) |
| `scraping_provider` | Returned by a scraping aggregator | **Unreviewed** | Blocked | Blocked (U-05) |
| `ai_derived` | Produced by a model from other fields | Follows the most restrictive input source | Follows inputs | Applies from phase 07 |

## Rules

1. **Imported is not verified.** Workbook assertions are stored as `user_import` with verification state `unverified`. They are never relabelled as fresh website verification.
2. **A URL is not a licence.** Keeping a source URL does not grant rights to copy the provider's data behind it.
3. **Listing identifiers are typed.** `place_id` and `cid` are stored separately. Only `place_id` has a documented storage exemption under the global terms; `cid` is retained solely as part of a user-imported URL.
4. **Restricted raw responses are never persisted** in logs, snapshots, prompts or export caches.
5. **Unclear rights mean quarantine.** Fields whose storage or export rights are unknown are kept out of ordinary CRM views and exports until resolved.
6. **Derived scores inherit restrictions.** A score component based on restricted data may only be persisted if the source policy allows it; otherwise activity evidence must come from an independent source.

## Google Places — open items (owner CRM-075)

The earlier review quoted the global Maps Platform terms. Bulgaria is in the EEA, where separate service terms apply. Before any live ingestion:

- Confirm the account billing region and which terms apply.
- Read the EEA Maps service terms for Places: permitted uses, caching, attribution, AI-processing restrictions.
- Decide whether ratings and review counts may be used for scoring at all, and whether a derived activity score may be stored.

Until then the Places adapter is contract-tested only and live discovery is `BLOCKED`.

## Reference workbook

Stored only in `tests/fixtures/private/` (git-ignored). Its rows contain real business contacts and must not appear in commits, logs, CI output or documentation. Aggregate counts are safe to publish.
