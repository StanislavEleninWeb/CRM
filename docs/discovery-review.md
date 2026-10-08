# SEWEB CRM — review of discovery proposal v2

Reviewed 8 October 2026 against `SEWEB-CRM-Discovery-v1.md` (header says "v2") and `SEWEB_prospects_Bulgaria_2026-10-07.xlsx`. Every disagreement below is backed by the workbook data or a fetched primary source. Nothing here is legal advice.

## Decisions confirmed after review

- **Mailbox provider (decision 1): Google Workspace.** `seweb.co` is on Google Workspace (Google MX and SPF records; confirmed by the owner).
- **Research source (decision 4): Google Places API first, a scraping provider later.** Point 4 applies as written to the Places API. The scraping provider's terms have not been checked and must be reviewed before that phase starts.

## Disagreements

### 1. Calling should come before email sequences (§3 B, decision 2)

The document makes mailbox integration and sequences the centrepiece of phase B and treats calling as a tel: link. The workbook points the other way:

| Measure (94 leads) | Count |
|---|---|
| Recommended channel is phone | 65 |
| Have a public phone | 93 |
| Have a public email | 36 |
| No email found | 58 |
| No website and no email | 30 |
| Tier A with an email | 14 of 21 |

- **Change:** make the phase A daily queue call-first: call list, tel: link, fast outcome form, follow-up scheduling.
- **Change:** cut phase B email to manual send plus reply tracking first; sequences would serve at most 38% of this dataset.

### 2. Scoring is assigned to the wrong phase (§5A)

- The document puts XLSX import in phase A and scoring in phase B.
- Its own phase A acceptance criteria require recomputing scores from components and reproducing the 21/62/11 split.
- **Change:** move the deterministic rubric (sum of five components, 80/60 thresholds) into phase A; keep AI-generated scoring in B.

### 3. Google mailbox access carries a cost gate in phase C (decision 1)

- An OAuth app set to "Internal" in SEWEB's own Workspace needs no verification and has no user cap, so Google is cheap for the pilot.
- `gmail.readonly`, `gmail.modify` and `gmail.metadata` are restricted scopes; `gmail.send` is only sensitive.
- Google states: "If you store restricted scope data on servers (or transmit), then you must go through a security assessment", revalidated every year. Google publishes no price, so get an assessor quote.
- For comparison, Microsoft's publisher verification is free and involves no third-party assessment.
- **Change:** add "Google restricted-scope verification and annual security assessment" as an explicit phase C gate before any external tenant connects Gmail. The document doesn't mention it.

### 4. "Applicable provider retention rules" is too vague to build from (§5A)

The Google Places API terms are specific:

- **Maps Platform Terms §3.2.3(a):** the customer will not "copy and save business names, addresses, or user reviews".
- **Service Specific Terms §14.3:** only latitude/longitude may be cached, for 30 days. `place_id` may be stored indefinitely.
- **Change:** store `place_id`, take identity and contact fields from the business's own website, and refetch review counts at scoring time instead of storing them. The workbook already works this way: all 94 Maps URLs are `place_id` links.
- **Not verified:** the terms that apply to a scraping provider (for example Apify). Check them before the scraping phase.

### 5. Bulgarian law requires three product features, not just a footnote

Electronic Commerce Act, Art. 6 (2019 English consolidation on the ministry site; unofficial and possibly amended since):

- **Art. 6(1):** unsolicited commercial email must be clearly identified as such. This needs a template-level label.
- **Art. 6(2)–(3):** sending to legal-person addresses on the Commission for Consumer Protection's opt-out register is forbidden. This needs a regulatory suppression list that can be imported and checked.
- **Art. 6(4):** sending to consumers without prior consent is forbidden. This needs a recipient-type field (legal person, sole trader, natural person) that blocks sends.
- **Art. 24:** fines for breaching these.

Of the 36 emails in the workbook, 19 are free-mail addresses (10 abv.bg, 8 gmail.com), where recipient type is ambiguous. Whether the register is currently operating is unconfirmed; the sources found date from 2007–2016. Confirm with a lawyer or the Commission.

### 6. Celery delayed tasks can break the "no duplicate sends" gate (§6, §11)

- Celery's Redis documentation says tasks with an ETA or countdown longer than the visibility timeout (default 1 hour) will "be executed again, and again in a loop".
- Follow-ups are scheduled days ahead, so they must never be Celery ETA tasks.
- **Change:** keep Celery, but have a poller claim due rows from `scheduled_sends` with `FOR UPDATE SKIP LOCKED` and a per-send idempotency key.

### 7. "Backfill after missed notifications" needs numbers (§3 B)

- **Gmail:** `watch` must be renewed at least every 7 days (Google recommends daily). Notifications are capped at one per second per user and can be dropped, so `history.list` is the source of truth.
- **Microsoft Graph (if added later):** message subscriptions expire in under 7 days (1 day with resource data). The lifecycle `missed`, `reauthorizationRequired` and `subscriptionRemoved` events must be handled.

## Import contract details the document misses

- **Contact types:** 17 phone cells hold several numbers separated by ";". Two carry labels, "delivery" and "emergency"; "emergency" is not in the document's contact-type list.
- **"Not found":** 176 literal values across five columns (website 30, phone 1, email 58, contact page 46, other channel 41).
- **Dates:** "Date checked" is a midnight datetime. Store it as a date to avoid timezone shifts.
- **Free-text columns:** website status has 14 distinct values and contact channel has 4 (including "Phone (ask for the manager)"). Both need an enum plus the raw value.
- **Tags:** the nine categories in the document plus "Other" (2 rows).
- **Sheet name:** the shortlist sheet is "Tomorrow’s outreach shortlist" with a curly apostrophe; match it tolerantly.
- **Currency:** default to EUR. Bulgaria adopted the euro on 1 January 2026 at 1 EUR = 1.95583 BGN.
- **Version label:** the file is named `-v1` but its header says "v2".

## Checked and agreed

- **Workbook counts:** 94 rows, 35 columns, unique IDs; 21/62/11 tiers; 31/58/5 confidence.
- **Scores:** every total equals its component sum, no component exceeds its cap, and the 80/60 thresholds hold at the boundary scores (80, 79, 60, 59 all occur).
- **Shortlist:** exactly the top 25 by score (21 A plus 4 B, minimum 77), built with lookups into the master sheet, so "import once, shortlist is a view" is right.
- **Safety:** no macros or external links. Totals and tiers are formulas, so reading cached values is correct.
- **Sending limits:** Workspace allows 2,000 messages a day per user. That does not constrain about 25 sends a day; treat it as a rate-limiter input.
- **Architecture:** no documented problem found with the modular monolith, row-level security design, FastAPI, or the tel:-first calling approach.

## Sources

- [Gmail API scopes](https://developers.google.com/gmail/api/auth/scopes)
- [Google: when verification is not needed](https://support.google.com/cloud/answer/13464323)
- [Google: security assessment](https://support.google.com/cloud/answer/13465431)
- [Microsoft publisher verification](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview)
- [Google Maps Platform Terms](https://cloud.google.com/maps-platform/terms)
- [Maps Service Specific Terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)
- [Places API policies](https://developers.google.com/maps/documentation/places/web-service/policies)
- [Bulgarian Electronic Commerce Act (English, 2019)](https://www.mtc.government.bg/sites/default/files/electronic_commerce_act-en_kym_26.02.2019.pdf)
- [Celery: Redis broker caveats](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
- [Gmail push notifications](https://developers.google.com/workspace/gmail/api/guides/push)
- [Microsoft Graph subscription lifetimes](https://learn.microsoft.com/en-us/graph/api/resources/subscription)
- [Workspace sending limits](https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace?hl=en)
- [European Commission: Bulgaria adopts the euro](https://trade.ec.europa.eu/access-to-markets/en/news/bulgaria-adopts-euro-1-january-2026)
