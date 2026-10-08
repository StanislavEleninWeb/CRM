# Provider setup

Configuration and verification instructions. No secrets belong in this file. Status values: `IMPLEMENTED`, `VERIFIED_LOCALLY`, `VERIFIED_IN_SANDBOX`, `VERIFIED_LIVE`, `NOT_STARTED`, `BLOCKED`.

| Provider | Purpose | Status | Blocked by |
|---|---|---|---|
| Local development OIDC | Sign-in for development and tests only | NOT_STARTED | — |
| Production OIDC | Sign-in for hosted environments | BLOCKED | U-01 |
| Google Workspace / Gmail (Internal app) | Manual send and reply tracking for SEWEB | NOT_STARTED | U-02 for live use |
| Google Places API | Business discovery | NOT_STARTED | U-04 for live use |
| AI model provider | Score proposals and drafts | NOT_STARTED | U-03 for live use |
| Stripe Billing (test mode) | Subscriptions | NOT_STARTED | U-08 |
| S3-compatible storage | Files and exports | NOT_STARTED | — |

Setup steps are added to this file as each provider is implemented.

## Environment variables

Names only; see `.env.example`. Values are injected at deploy time and never committed.
