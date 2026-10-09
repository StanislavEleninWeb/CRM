# Outreach policy

Versioned channel and recipient eligibility rules. The enforced rules live in code and the `outreach_policies` table (CRM-084); this document records their basis and how fresh that basis is. Nothing here is legal advice.

## Policy version

| Field | Value |
|---|---|
| Policy version | `bg-2026-10-draft` |
| Status | Draft — not approved for live unsolicited email |
| Regulatory source | Bulgarian Electronic Commerce Act, Article 6 |
| Source checked | 8 October 2026, 2019 English consolidation (unofficial; may be superseded) |
| Qualified review | Not done (U-06) |

## Concepts kept separate

- **Legal form:** legal person, sole trader, natural person, unknown.
- **Recipient context:** business, consumer, unknown.
- **Consent or request:** scope, source and date of any prior consent or explicit request.
- **Lawful basis** for processing personal data.
- **Channel permission:** per-channel do-not-contact state with a reason.

A free-mail domain is not evidence of legal form. Unknown facts route to review; they are never resolved by guesswork or by a model.

## Phone (phase A)

- Call-specific do-not-contact flags are honoured.
- Numbers with purpose `emergency` are excluded from sales dialling by default.
- A callback request is recorded with how and when it was made. It does not imply consent for unrelated campaigns.
- Opening the dialler is not a completed call.

## Email (phase B, draft rules)

| Situation | Decision |
|---|---|
| Recipient on local suppression list | Block, always |
| Recipient is a natural person or consumer without recorded consent | Block |
| Legal form or context unknown | Review required |
| Unsolicited message to a legal person | Requires a clear check against the regulatory opt-out register, the "unsolicited commercial communication" identification, and sender identity |
| Regulatory register check stale or unavailable | Block unsolicited sending; an outage is never treated as an empty register |
| Individually approved reply to an incoming message | Allowed under the reply path; not treated as unsolicited outreach |
| Requested or consented message | Allowed only through an explicit recorded policy path |

## How the rules are enforced (phase 08)

- Each workspace starts with the draft policy. While it is a draft, unsolicited email is blocked.
- Only the owner can approve. Approval records who reviewed the law and the register process, the date the source was checked, the label wording and how old a register may be. Each approval creates a new version; every eligibility decision stores the version it used.
- The opt-out register is imported as a file obtained through its official channel. The application does not fetch it. No register, or one older than the approved age, blocks unsolicited email.
- Recipient type is recorded by a person with evidence. Without it the message goes to review.
- The label, sender identification and opt-out link are appended by the server when the message is rendered; editing the draft cannot remove them. If the label wording is empty the message is blocked.
- The opt-out link is signed. Opening it shows a confirmation button, so a mail scanner cannot opt someone out; the one-click POST form used by mail clients works without a session.
- A permanent delivery failure suppresses the address; a temporary one does not. Suppressions cannot be deleted, only lifted with a note, and re-importing a list does not undo them.
- A follow-up the prospect asked for on a call is allowed only for the address and scope recorded with that call.

## Open items before live unsolicited email

1. Confirm the current text of Article 6 and how sole traders are treated.
2. Establish whether the opt-out register is operating, how it is accessed, and how fresh a check must be.
3. Approve the template identification wording in Bulgarian.
4. Record who approved the policy and when.
