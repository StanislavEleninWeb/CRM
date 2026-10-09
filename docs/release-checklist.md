# Release checklist and pilot handoff

State on 9 October 2026, after build phases 00–14. **Nothing is deployed and no external service has ever been contacted**: no email sent, no call placed, no paid API used, no Stripe or Google account connected. Everything marked PASS was verified on a developer machine against PostgreSQL, with local stand-ins for outside services.

Words used here: **PASS** — verified by a test or check that ran, named in `test-evidence.md`. **BLOCKED** — cannot be verified until someone outside the build supplies something. **NOT RUN** — could have been verified but was not. **FAIL** — verified and wrong (none at present).

## What can be used now, by capability

| Capability | Software | Provider | Policy or approval | Live test | Usable by the SEWEB pilot |
|---|---|---|---|---|---|
| **Call-first core** — import, scoring, verification, call queue, manual call outcomes, follow-ups, opportunities, reports, export | PASS | none needed | none needed | not applicable | **For evaluation only, by one person at the computer it runs on**, using the built-in test logins. **Not yet usable by the people who will call**: they need it on their phones under their own accounts, which needs hosting (U-07) and a real sign-in provider (U-01) |
| **AI research** | PASS with stand-ins | BLOCKED — no AI provider chosen (U-03); Google Places terms for an EEA-billed account unverified (U-04) | Places: only the place ID is stored until the terms are confirmed | NOT RUN | No. Runs with made-up local data only |
| **Internal Gmail** — connect SEWEB's mailbox, read replies, send one approved email | PASS with stand-ins | BLOCKED — mailbox and Google Cloud project not confirmed (U-02) | BLOCKED — Bulgarian outreach rules are a draft awaiting qualified review and owner approval (U-06) | NOT RUN. `EMAIL_DISPATCH=off` | No. Drafting and eligibility preview work; nothing can be sent |
| **External Gmail** (other organisations) | **Not built.** Gate closed in code | BLOCKED — Google verification and security assessment not started; no quote (U-09) | — | — | **Disabled.** Every connect path refuses; see `external-gmail-gate.md` |
| Public API and webhooks | PASS; examples run over HTTP locally | webhook delivery verified against a mocked receiver only | — | NOT RUN against a real receiver | Yes locally |
| Billing | PASS against a stand-in | BLOCKED — no Stripe account, no approved plans or prices (U-08). Seeded plans are labelled tests | tax and invoicing not addressed | NOT RUN | Not needed for the pilot: `BILLING_MODE=off` |
| Email sequences | Not built (deferred to phase 15) | — | — | — | Not a release blocker |

## Gates

| Gate | Status | Evidence |
|---|---|---|
| Two-workspace isolation as the runtime database role | PASS | Schema guard over every tenant table; per-phase isolation tests; `test_release.py`: 66 identifier-bearing route and method combinations called from another workspace with real identifiers **and valid request bodies** — 58 answer 404 and the other 8 answer that the record does not exist; none succeeds and nothing changes; search, filters and bulk; 360 concurrent requests over a shared connection pool; worker acting for the wrong tenant; exports; webhooks; provider secrets copied between tenants unreadable. Three multipart upload routes are outside the sweep and have their own tests |
| Reference workbook: 94 leads, 25 shortlist rows, tiers 21/62/11, confidence 31/58/5, markers, date-only fields, identifier types, contact normalisation | PASS locally with the private file (7 tests). **Skipped in public CI by design** | `pytest -m reference_fixture` |
| Re-import and refresh do not overwrite outreach status or history | PASS for re-import. Research "refresh existing" mode is **not built** (CRM-074) | `test_import.py`, `test_research.py` |
| Whole journey: onboarding → import → verify → queue → call outcome → callback due → research → promote → draft → approve → send → reply stops pending follow-up → deal won → report → export | PASS with stand-ins | `test_release.py::test_the_whole_journey…` |
| Send reliability: one send under concurrent workers; no lock across the provider call; uncertain sends reconciled and never resent; worker death; lost poller | PASS | `test_sends.py` |
| Suppression: opt-out, reply, bounce, stale register, erased business — before dispatch | PASS | `test_sends.py`, `test_email.py`, `test_ops.py` |
| Failure rehearsals | PASS — see the table below | |
| Invalid workbooks; prompt injection | PASS | `test_import.py`, `test_research.py` |
| List and report speed at a realistic volume | PASS for the pilot's size; **will need work before about 5,000 prospects** — 1,500 prospects imported with their assessments, scores and channels, 1,000 calls, on a laptop: prospect list 370 ms, call queue 500 ms, everything else under 50 ms. Both slow ones grow with the number of prospects | `test_release.py::test_lists_and_reports…` |
| Mobile layout and labelled controls | PASS for fifteen pages at 375 px, including the prospect page with its email panel (no overflow, no unlabelled input, no error state). Not an accessibility audit: no screen reader, no contrast measurement, no keyboard-only pass | Browser check, this phase |
| Backup restores into an isolated database | PASS locally, encrypted | `infra/backup/restore-check.sh` |
| Upgrade from the first published schema with data | PASS locally | `infra/checks/upgrade-from-previous.sh` |
| Production images build and pass image checks | **NOT RUN at the current commit** — registry lookups timed out; unchanged stages built on GitHub for PR 2 | Phase 13 evidence |
| Release pipeline; staging deployment; rollback rehearsal | **NOT RUN / BLOCKED** — no host (U-07) | |
| CI on GitHub for the current commits | **NOT RUN** — phases 09–14 are local commits. PR 2's backend job failed at lint on a cache-permission problem that is fixed locally and unverified on GitHub | |
| Production sign-in and MFA | BLOCKED (U-01) | |

## Failure rehearsals

| Failure | What happens | Test |
|---|---|---|
| The scheduler-to-worker path as it really runs (separate containers) | Import, scoring, queue and calls (47 checks, private workbook), a research run (12), the API examples (18), each on a fresh stack | `infra/e2e/phase_a.py`, `phase_b_research.py`, `phase_c_api.py`, re-run in this phase |
| The same task delivered twice, or eight workers at once | One send, one activity | `test_many_workers_and_duplicate_tasks_send_one_message` |
| Scheduler ticks twice; many pollers | Each due row is claimed once | `test_research.py` (eight pollers, forty rows), `test_sends.py` |
| Poller dies after claiming | Reclaimed after the lease; sent once; late task rejected | `test_a_poller_that_dies…` |
| Worker dies mid-send | Marked unknown, mailbox checked, not resent | `test_a_worker_that_dies…` |
| Mailbox notification lost, duplicated or reordered | Recovered by the 15-minute check; cursor never jumps | `test_notifications_are_only_triggers…` |
| History position expired | Full resynchronisation, no duplicates | `test_an_expired_cursor…` |
| Member removed; key revoked or expired | Access ends at once | `test_identity.py`, `test_api_access.py` |
| Provider says slow down | Backs off for the stated time; bounded retries | `test_sends.py`, `test_email.py`, `test_providers.py` |
| Opt-out racing a send | Never a deadlock; the opt-out is always recorded | `test_an_opt_out_and_the_final_check…` |
| Twenty threads on one budget | Never over the limit | `test_providers.py` |
| Eight invitations for two seats | Exactly two succeed | `test_billing.py` |
| Billing events duplicated and out of order | State equals the provider's | `test_billing.py` |
| Server error after doing the work | The same idempotency key is refused, not rerun | `test_api_access.py` |

## Known limitations that matter to a pilot

- **Research "refresh existing" is not built**; the request is refused with a message. Research inspects the homepage only and does not read `robots.txt`.
- **Call outcomes are what a person reports.** Nothing is confirmed by a telephone provider.
- **"Accepted by the mailbox provider" is not delivery.** Only replies and bounces are evidence.
- Reports filter by period and by member (the member filter has no control in the interface yet); not by city or service.
- After a restore from backup, erasures and opt-outs made since the backup must be re-applied by hand from a record kept outside the database.
- No error-tracking service; no off-host backup destination; three container images pinned by tag rather than digest.
- The outreach rules are the owner's to approve. The software enforces them; it does not establish that they are legally right.

## Backup, restore and rollback

`docs/runbooks/backup-restore.md` and `docs/runbooks/deployment.md`. Day-to-day problems: `docs/runbooks/operations.md`.

## Trying it on one machine

This is for one person to evaluate the product. It is **not** how the pilot's callers will use it: the built-in sign-in knows only six fixed test users (`owner@`, `admin@`, `manager@`, `rep@`, `viewer@`, `other@example.test`), an invitation can only be accepted by one of those addresses, the stack must not be reachable from other machines, and the call buttons are meant for a phone.

```bash
make up
```

1. Open `http://localhost:5173`, sign in as `owner@example.test` (development identity provider; password in `infra/dex/config.dev.yaml`), create the workspace.
2. **Import** → upload the prospect workbook → review the preview → commit.
3. **Team** → to see the roles at work, invite one of the other test users (for example `rep@example.test`) and sign in as them in a private window. Roles: owner, administrator, sales manager, representative, read-only.
4. **Today** is the daily screen: what needs attention, then the call queue. Tap a number to open the phone's dialler, then say what happened.

This is a development stack: the sign-in provider has fixed test users and must not be exposed beyond the machine. After an import the local database holds real prospect data; `make reset` removes it.

## Example journeys

- **Representative, morning.** Today → follow-ups due first → call → "No answer" (returns another day) or "Follow-up requested" with a date and what they asked for → next prospect.
- **Manager, verification.** Verification → read the finding and its source → Verified or Contradicted with a note. Imported claims stay "unverified" until someone does this.
- **Manager, a reply arrives** (once Gmail is connected). Today shows "Replies waiting" → the prospect's page shows the conversation → mark the reply positive, neutral or negative → draft a reply → approve → send.
- **Owner, month end.** Reports → pick the period → each figure states what it counts; "No data" where there is nothing to divide by → Show records to see what is behind a figure.
- **Owner, a business asks to be forgotten.** Company → erase, with the reason. It is removed, will not be emailed, and will not come back through an import.

## The smallest next actions, in order

1. **You: choose where it runs (U-07) and how people sign in (U-01).** This is what stands between the build and the callers using it on their phones. With a host: rebuild the images, run the release workflow against staging, rehearse a rollback.
2. **You, separately: say whether to push.** Phases 09–14 exist only as local commits on `build/core`. Pushing updates pull request 2 and is the only way to learn whether CI passes on GitHub; the first run also builds the production images and runs the image checks that could not be run locally.
3. **You: confirm the outreach mailbox and that the Google Cloud project belongs to the SEWEB organisation (U-02).** Then one dry run and one real email to an address you control, as described in `provider-setup.md`.
4. **You: have the Bulgarian outreach rules reviewed and approve them in the application (U-06).** Unsolicited email stays blocked until then.
5. **You: choose the AI provider and monthly budget (U-03) and confirm the Places billing region (U-04)** for live research.
6. **Later: Stripe test account and approved plans (U-08); external Gmail verification and assessment (U-09).**
7. **Engineering, no input needed:** research refresh mode (CRM-074); pin three images by digest; a member filter control on Reports; make the prospect list and call queue independent of the total number of prospects.
