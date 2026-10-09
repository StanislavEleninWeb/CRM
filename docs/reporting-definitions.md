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
| Follow-ups due | Tasks that a reported call created for a follow-up (or of kind "follow-up"), due in the period, not cancelled | — | Application |
| Follow-ups completed | Of those, the ones marked done | Follow-ups due | Reported by people |
| Emails accepted | Messages the mailbox provider accepted | — | Mailbox. Acceptance is not delivery |
| Replies received | Incoming messages classified as replies (not automatic replies or bounces) | — | Mailbox |
| Median time to answer a reply | Hours from a reply to the next message sent in that conversation | Replies that were answered | Mailbox. Unanswered replies are excluded, so this flatters slow responders |
| Replies judged positive | Conversations a person marked positive in the period | Conversations a person judged in the period | Reported by people. Unjudged replies are in neither number |
| Meetings booked | Tasks of kind "meeting" created in the period, not cancelled | — | Reported by people |
| Proposals | Opportunities moved into a stage flagged as a proposal stage | — | Reported by people. A stage whose name starts with "Proposal" is flagged automatically; the flag can be changed |
| Opportunities won | Deals closed in the period in a "won" stage | — | Reported by people |
| Stage table | Deals entering each stage in the period; open deals now, with average and longest time in the current stage | — | Application |
| Won amounts | Sum of amounts of won deals, **one line per currency** | — | As entered |
| Research cost per qualified lead | Research costs recorded in the period (estimated and verified) | Qualified leads created in the period whose source is research | Usage ledger |

Rules:

- A rate with a zero denominator is shown as **No data**, never 0% or 100%.
- Amounts in different currencies are never added. If research costs span currencies, no per-lead figure is shown.
- Costs still reserved, or whose charge is unknown after a provider timeout, are listed separately and are not in the per-lead figure.
- Nothing is confirmed by a telephone provider. Call figures are what people reported.

Three figures exist only because a person records them, and are never inferred from the wording of a message or from a model's reading of it: whether a reply is positive, that a meeting was booked, and that a proposal was made.

Not measured, and stated as such in the interface:

- **Opens and clicks.** Not collected.
- **Delivery.** Only acceptance by the mailbox provider, replies and bounces are known.
- **Per-person mailbox figures.** Emails and replies are counted for the whole workspace.

Known limits: figures are computed on request without caching; the records list behind a figure shows at most 200 entries; filters are the period and, for figures about leads, calls, tasks and opportunities, one member (`owner_user_id`); there is no filter by city or service, and the member filter is not yet offered in the interface.
