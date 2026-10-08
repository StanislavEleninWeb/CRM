# Provider setup

Configuration and verification instructions. No secrets belong in this file. Status values: `IMPLEMENTED`, `VERIFIED_LOCALLY`, `VERIFIED_IN_SANDBOX`, `VERIFIED_LIVE`, `NOT_STARTED`, `BLOCKED`.

| Provider | Purpose | Status | Blocked by |
|---|---|---|---|
| Local development OIDC (Dex 2.46) | Sign-in for development and tests only | VERIFIED_LOCALLY | — |
| Production OIDC | Sign-in for hosted environments | BLOCKED | U-01 |
| Google Workspace / Gmail (Internal app) | Manual send and reply tracking for SEWEB | NOT_STARTED | U-02 for live use |
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
