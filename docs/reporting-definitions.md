# Reporting definitions

Every figure on the Reports page carries its definition in the interface; this is the same list for reference. The period is whole days in the workspace time zone, inclusive at both ends. Each figure counts events by **their own date**, so figures in one period are not a cohort of the same leads.

Three things that are kept apart:

- **Qualification** of a lead (`needs_review`, `qualified`, `disqualified`): a judgement about fit.
- **Communication status** (`not_contacted`, `attempted`, `contacted`, `follow_up`, `emailed`, `replied`, …): what has happened between you.
- **Opportunity stage**: where a deal stands.

| Figure | Counts | Divided by | Source |
|---|---|---|---|
| Qualified leads | Leads created in the period whose qualification is `qualified` now | — | Application |
| Dialler opened | Times the dialler was opened from the application | — | Application. **Not calls** |
| Calls reported | Call attempts with an outcome reported in the period | — | Reported by people |
| Connected-call rate | Reported calls where someone was reached (connected, follow-up requested, not interested) | All calls with a reported outcome | Reported by people. Dialler openings are not in the denominator |
| Asked to be contacted again | Reported calls with outcome "follow-up requested" | — | Reported by people |
| Follow-ups due | Follow-up tasks due in the period, not cancelled | — | Application |
| Follow-ups completed | Of those, the ones marked done | Follow-ups due | Reported by people |
| Emails accepted | Messages the mailbox provider accepted | — | Mailbox. Acceptance is not delivery |
| Replies received | Incoming messages classified as replies (not automatic replies or bounces) | — | Mailbox |
| Median time to answer a reply | Hours from a reply to the next message sent in that conversation | Replies that were answered | Mailbox. Unanswered replies are excluded, so this flatters slow responders |
| Opportunities won | Deals closed in the period in a "won" stage | — | Reported by people |
| Stage table | Deals entering each stage in the period; open deals now, with average and longest time in the current stage | — | Application |
| Won amounts | Sum of amounts of won deals, **one line per currency** | — | As entered |
| Research cost per qualified lead | Research costs recorded in the period (estimated and verified) | Qualified leads created in the period whose source is research | Usage ledger |

Rules:

- A rate with a zero denominator is shown as **No data**, never 0% or 100%.
- Amounts in different currencies are never added. If research costs span currencies, no per-lead figure is shown.
- Costs still reserved, or whose charge is unknown after a provider timeout, are listed separately and are not in the per-lead figure.
- Nothing is confirmed by a telephone provider. Call figures are what people reported.

Not measured, and stated as such in the interface:

- **Booked meetings.** There is no meeting record. A meeting is never inferred from the wording of a reply or from a model's reading of it.
- **Proposals.** Only as deals entering a stage, if the pipeline has such a stage.
- **Positive replies.** Nobody marks a reply as positive, and it is not inferred.
- **Opens and clicks.** Not collected.

Known limits: figures are computed on request without caching; the records list behind a figure shows at most 200 entries; there are no filters other than the period (by owner, city or service would need the same queries with one more condition).
