# Provider setup

Configuration and verification instructions. No secrets belong in this file. Status values: `IMPLEMENTED`, `VERIFIED_LOCALLY`, `VERIFIED_IN_SANDBOX`, `VERIFIED_LIVE`, `NOT_STARTED`, `BLOCKED`.

| Provider | Purpose | Status | Blocked by |
|---|---|---|---|
| Local development OIDC (Dex 2.46) | Sign-in for development and tests only | VERIFIED_LOCALLY | — |
| Production OIDC | Sign-in for hosted environments | BLOCKED | U-01 |
| Google Workspace / Gmail (Internal app) | Manual send and reply tracking for SEWEB | IMPLEMENTED (contract-tested against a mocked transport; never connected to a real mailbox) | U-02 for live use |
| Google Places API | Business discovery | NOT_STARTED | U-04 for live use |
| AI model provider | Score proposals and drafts | NOT_STARTED | U-03 for live use |
| Stripe Billing (test mode) | Subscriptions | IMPLEMENTED (contract-tested against a mocked transport; never run against Stripe) | U-08 |
| S3-compatible storage | Files and exports | VERIFIED_LOCALLY (SeaweedFS) | Production bucket not chosen |

Setup steps are added to this file as each provider is implemented.

## Environment variables

Names only; see `.env.example`. Values are injected at deploy time and never committed.

## Identity provider (OIDC)

The API is a confidential OIDC client using the authorization-code flow with PKCE.

| Variable | Meaning |
|---|---|
| `OIDC_ISSUER` | Exact `iss` value; also the address the browser is sent to |
| `OIDC_INTERNAL_URL` | Optional. How the API reaches the provider when it differs from the public address |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | Client registered at the provider |
| `OIDC_DEV_PROVIDER` | `true` only for the bundled development provider; refused in staging and production |

Register the redirect URI `<PUBLIC_BASE_URL>/api/v1/auth/callback`. Staging and production require `https` for both the issuer and the public base URL.

**Development provider.** `infra/dex/config.dev.yaml` defines six static users (`owner@`, `admin@`, `manager@`, `rep@`, `viewer@`, `other@example.test`) with the password `dev-password`. It uses in-memory storage and must never be exposed outside a developer machine.

**Choosing the production provider (U-01).** It must support OIDC discovery, RS256/ES256/PS256 signatures, PKCE, a verified-email claim, and MFA. Invitations are accepted only by a user whose verified email matches the invitation.

## Provider credentials and encryption keys

Provider API keys are encrypted with AES-256-GCM before they are stored. The key material is in `SECRET_ENCRYPTION_KEYS`, never in the database.

- Format: comma-separated `version:base64key`, newest first. Each key is 32 random bytes: `openssl rand -base64 32`.
- `make up` generates a local key the first time. Staging and production must set their own and keep it in their secret store.
- Losing every listed key makes stored credentials unreadable; they then have to be re-entered. Back the key up separately from the database.

**Rotation**

1. Add a new key at the front: `SECRET_ENCRYPTION_KEYS=v2:<new>,v1:<old>` and restart the services.
2. Run `docker compose exec api python -m app.modules.providers.rotate`. It re-encrypts every tenant's credentials and exits non-zero if any could not be read.
3. When it reports nothing left on the old version, remove the old key and restart.

**Access modes**

| Mode | Who pays the provider | Budget control |
|---|---|---|
| Bring your own key | The tenant, on their own provider account | Application budget still applies |
| Platform-managed | The platform, from an included or prepaid allowance | Application budget applies; no overage unless explicitly allowed |
| OAuth | Depends on the provider | Depends on the provider |

A consumer chat subscription with an AI vendor does not include API access. An API key comes from the vendor's developer console and is billed separately.

**Costs shown in the application** are either *confirmed by the provider* or *estimated here from list prices*. They are reported separately. An operation that timed out keeps its budget reserved until an owner checks the provider's records and resolves it.

## Gmail (internal pilot)

Only the organisation that owns the Google Cloud project may connect a mailbox. Nothing below has been done against a real Google account; the steps follow Google's documented flow and must be confirmed during the live gate.

| Variable | Meaning |
|---|---|
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` | OAuth client of an app whose user type is **Internal** in the SEWEB Workspace |
| `GMAIL_INTERNAL_DOMAIN` | The Workspace domain. The `hd` claim of the signed identity token must equal it; an address suffix alone is not accepted |
| `GMAIL_INTERNAL_TENANT_IDS` | Comma-separated workspace IDs allowed to use this app. Any other workspace is refused |
| `GMAIL_PUBSUB_TOPIC` | Full topic name for mailbox notifications. Optional: without it replies are still collected every 15 minutes |
| `GMAIL_PUSH_AUDIENCE`, `GMAIL_PUSH_SERVICE_ACCOUNT` | Audience and service account expected on the signed push request |
| `MAILBOX_RECONCILE_MINUTES` | Timed check that recovers lost notifications (default 15) |
| `EXTERNAL_GMAIL_ENABLED` | Leave `false`. External Gmail is not implemented (CRM-114) |
| `EMAIL_DISPATCH` | `off` (default): send requests are refused. `dry_run`: every check runs and nothing is sent. `live`: approved messages are sent |
| `SEND_LEASE_SECONDS`, `SEND_SCHEDULE_MAX_DAYS` | How long a worker may hold a send (default 90 s); how far ahead a message may be scheduled (default 60 days) |

Steps for the owner (U-02):

1. In the Google Cloud project owned by the SEWEB organisation, set the OAuth consent screen user type to Internal.
2. Create a web OAuth client with the redirect URI `<PUBLIC_BASE_URL>/api/v1/mailboxes/gmail/callback`.
3. Enable the Gmail API. Requested scopes: `openid`, `email`, `gmail.send`, `gmail.readonly`. Mailbox modification is not requested.
4. Optional: create a Pub/Sub topic, grant Gmail's push service account publish rights, and add a push subscription to `<PUBLIC_BASE_URL>/api/v1/webhooks/gmail` with OIDC authentication.
5. Set the variables above, restart, and connect the mailbox under Integrations. The mailbox stays marked "not yet confirmed against a real mailbox" until the live gate is recorded.

The refresh token is encrypted with the same keys as provider credentials. Attachments are never downloaded.

### Sending limits

Each mailbox has a daily limit (default 50) and a minimum gap between messages (default 30 seconds). These are cautious product defaults stored on the mailbox row, not Google's own limits, which must be checked for the account before raising them. A message over the limit waits in the database and is sent when the limit allows.

### First live send (not yet done)

1. Complete the Gmail steps above and the outreach-rule approval in the Email page.
2. Set `EMAIL_DISPATCH=dry_run`, send one message to an address you control, and confirm it ends as "Dry run finished".
3. Set `EMAIL_DISPATCH=live`, send the same message, reply to it, and confirm the reply appears on the prospect. Record the result in `docs/test-evidence.md`.

## Inbound callbacks

A provider callback is trusted only after its own verification, and it is mapped to a tenant by data this application stored, never by anything in the callback.

| Callback | Verification | Tenant mapping | Status |
|---|---|---|---|
| Gmail push (`/api/v1/webhooks/gmail`) | Google-signed OIDC token: issuer, audience and service account | The mailbox address looked up in `mailbox_routes`; the history ID in the message is never used as data | IMPLEMENTED, tested with a local signer |
| Stripe (`/api/v1/webhooks/stripe`) | Signature over the raw body, five-minute tolerance | The customer ID looked up in `billing_customers`; the payload's own tenant references are checked against it and never trusted | IMPLEMENTED, tested with a local signer |

## API keys and outbound webhooks

See `examples/api/README.md` for use. Operational notes:

- Keys and webhook signing secrets are shown once. A lost key is revoked and replaced; a lost secret is rotated (the old one stays valid for 24 hours).
- Webhook secrets are encrypted with `SECRET_ENCRYPTION_KEYS`; the rotation command re-encrypts them along with provider credentials and mailbox tokens.
- Outbound deliveries only go to public addresses over HTTPS (plain HTTP is accepted in development and test only). The address is resolved and checked at every delivery, and the connection is made to the address that was checked.

## Billing (Stripe, test mode)

Nothing below has been done; no Stripe account or approved price exists (U-08). A live key (`sk_live…`) is refused at start-up.

| Variable | Meaning |
|---|---|
| `BILLING_MODE` | `off` (default): no limits, nothing charged. `test`: trials, plans and limits apply against Stripe test mode |
| `STRIPE_SECRET_KEY` | A **test-mode** secret or restricted key |
| `STRIPE_WEBHOOK_SECRET` | Signing secret of the webhook endpoint |
| `TRIAL_DAYS`, `PAST_DUE_GRACE_DAYS` | Defaults 14 and 7 |

Steps for the owner, in Stripe test mode:

1. Create one recurring per-seat price for each plan to be sold. Attach each to its plan: `docker compose exec api python -m app.modules.billing.configure test_starter price_…`. A plan without a price cannot be bought.
2. Add a webhook endpoint for `<PUBLIC_BASE_URL>/api/v1/webhooks/stripe` with the events `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`.
3. Enable the customer portal (payment method, invoices, cancellation, plan change).
4. Set the variables, restart, and run the lifecycle by hand: subscribe with a test card, fail a payment, recover, cancel. Record the result in `docs/test-evidence.md`. Until then the status stays IMPLEMENTED.

Before live billing, and not addressed by this build: approved plans and prices, tax registration and collection, invoice content, refund policy, and Stripe account activation. The seeded plans are named and labelled as tests so they cannot be mistaken for an offer.
