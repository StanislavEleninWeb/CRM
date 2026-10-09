# Provider setup

Configuration and verification instructions. No secrets belong in this file. Status values: `IMPLEMENTED`, `VERIFIED_LOCALLY`, `VERIFIED_IN_SANDBOX`, `VERIFIED_LIVE`, `NOT_STARTED`, `BLOCKED`.

| Provider | Purpose | Status | Blocked by |
|---|---|---|---|
| Local development OIDC (Dex 2.46) | Sign-in for development and tests only | VERIFIED_LOCALLY | — |
| Production OIDC | Sign-in for hosted environments | BLOCKED | U-01 |
| Google Workspace / Gmail (Internal app) | Manual send and reply tracking for SEWEB | IMPLEMENTED (contract-tested against a mocked transport; never connected to a real mailbox) | U-02 for live use |
| Google Places API | Business discovery | NOT_STARTED | U-04 for live use |
| AI model provider | Score proposals and drafts | NOT_STARTED | U-03 for live use |
| Stripe Billing (test mode) | Subscriptions | NOT_STARTED | U-08 |
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

Steps for the owner (U-02):

1. In the Google Cloud project owned by the SEWEB organisation, set the OAuth consent screen user type to Internal.
2. Create a web OAuth client with the redirect URI `<PUBLIC_BASE_URL>/api/v1/mailboxes/gmail/callback`.
3. Enable the Gmail API. Requested scopes: `openid`, `email`, `gmail.send`, `gmail.readonly`. Mailbox modification is not requested.
4. Optional: create a Pub/Sub topic, grant Gmail's push service account publish rights, and add a push subscription to `<PUBLIC_BASE_URL>/api/v1/webhooks/gmail` with OIDC authentication.
5. Set the variables above, restart, and connect the mailbox under Integrations. The mailbox stays marked "not yet confirmed against a real mailbox" until the live gate is recorded.

The refresh token is encrypted with the same keys as provider credentials. Attachments are never downloaded.
