# External Gmail gate (CRM-114)

Whether an organisation other than the installation's owner may connect a Gmail mailbox. **The gate is closed and the external connection flow is not built.** This document records what must be true before that changes. Nothing here is a certification, and no cost is stated because no quote has been obtained.

## Why it is separate

The pilot uses a Google OAuth app whose user type is *Internal*: it serves one Google Workspace organisation and is exempt from Google's verification. That exemption cannot be stretched to other organisations. The application refuses it in code: an external connection needs its own client ID, different from the internal one.

## Scopes the product requests

| Scope | Why | Google's classification, to be confirmed at submission |
|---|---|---|
| `openid`, `email` | Identify the connecting account and its organisation | Non-sensitive |
| `gmail.send` | Send a message a person approved | Sensitive |
| `gmail.readonly` | Read replies so they appear on the prospect | Restricted |

Reply tracking reads mail. Sending manually does not reduce that: as long as replies are read automatically, the read scope and its requirements apply. A future *send-only* mode (no reply tracking, `gmail.send` alone) would be a distinct, clearly named capability with its own gate; it must not be presented as reply tracking.

## Data handled

| Data | Where | Retention |
|---|---|---|
| Refresh token | Encrypted in the database with keys held outside it | Until disconnect; erased on disconnect |
| Message headers, text, sanitised HTML | Database, per tenant | Tenant retention settings (phase 12) |
| Attachment names and sizes | Database | With the message |
| Attachment contents | Never downloaded | — |
| Mailbox history position | Database | While connected |

Message content is processed on the application's servers. Google's requirements for restricted scopes handled server-side therefore have to be assumed to apply unless Google confirms an exception in writing.

## Prerequisites, and their state

| # | Prerequisite | State | Owner |
|---|---|---|---|
| 1 | A separate Google Cloud project and OAuth client with user type External, with verified domain, branding, privacy policy and a consent screen describing the use above | Not started | Unassigned (U-09) |
| 2 | Google's verification for the sensitive scope | Not started | Unassigned |
| 3 | Google's verification for the restricted scope, including the security assessment Google requires when restricted data is handled on servers | Not started. **No assessor has been approached and no quote exists.** Budget and schedule owner: unassigned | Unassigned |
| 4 | Assessment validity date recorded, with a renewal reminder before it lapses | Nothing to record yet | Unassigned |
| 5 | The external connection flow built and tested: per-tenant consent, token storage, disconnect and deletion | Not built | Engineering |
| 6 | Product disclosure of what is read and stored, shown before connecting | Not written | Unassigned |

Microsoft mailboxes are out of scope here. Microsoft's publisher verification is a different process from a security assessment, and tenant-administrator consent is not automatic.

## How the gate is enforced

`app.modules.email.oauth.external_gmail_gate` returns what is missing. All of these must hold before it returns nothing:

- `EXTERNAL_GMAIL_ENABLED=true`
- `EXTERNAL_GMAIL_CLIENT_ID` set, and different from the internal client
- `EXTERNAL_GMAIL_VERIFICATION_REF` set: the reference of Google's approval
- `EXTERNAL_GMAIL_ASSESSMENT_VALID_UNTIL` set to a date that has not passed. When it passes, the gate closes again by itself.

Every path that could start or finish a connection — the availability check, the connect redirect and the OAuth callback — goes through the same admission check. A test walks each prerequisite and confirms all three stay closed; with every prerequisite recorded they still refuse, because the flow does not exist.

## Launching without it

The commercial product can launch with Gmail unavailable to external organisations, provided the product says so plainly: calls, research, the CRM, the API and webhooks work; connecting a mailbox does not.
