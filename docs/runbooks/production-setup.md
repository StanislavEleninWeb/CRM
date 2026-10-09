# Production setup: crm.seweb.co

Decided on 9 October 2026: one environment (production) on the VPS `161.97.89.47`, deployed by GitHub Actions, files and backups on AWS S3, sign-in through AWS Cognito, certificate notices to `management@seweb.co`.

**State: prepared. Nothing below has been done yet, and nothing has been deployed.** Each step says who does it. Secrets are typed on the server or into GitHub; they are never put in the repository or sent in chat.

Found by looking from outside, 9 October 2026:

- `crm.seweb.co` has **no DNS record** yet.
- The server **already runs Caddy on ports 80 and 443**. So this application does not start its own proxy: it listens on the server's loopback interface (`127.0.0.1:18000` for the API, `127.0.0.1:18080` for the pages) and the existing Caddy forwards to it.

## 1. DNS and Cloudflare — you

Done on 9 October: `crm.seweb.co` exists and is **proxied by Cloudflare** (it resolves to Cloudflare's addresses, not to the server). Decided: keep the proxy, for protection. That needs these settings in Cloudflare for `seweb.co`:

| Where | Setting | Why |
|---|---|---|
| SSL/TLS → Overview | **Full (strict)** | Cloudflare checks the server's certificate. With "Flexible" the traffic between Cloudflare and the server would be unencrypted |
| SSL/TLS → Origin Server | **Create an Origin Certificate** for `crm.seweb.co` (15 years is the default). Save the certificate as `origin.pem` and the key as `origin.key` | It is the certificate the server presents to Cloudflare. It is trusted by Cloudflare only, which is why the mode above works and why a direct visit to the server shows a certificate warning |
| SSL/TLS → Origin Server | **Authenticated Origin Pulls: on** | Cloudflare presents its own client certificate; the server refuses anyone else. Without this, anyone who learns the server's address can bypass Cloudflare |
| SSL/TLS → Edge Certificates | **Always Use HTTPS: on** | |
| Scrape Shield | **Email Address Obfuscation: off** for this name | It rewrites pages; a CRM shows many addresses |
| Speed → Optimization | **Rocket Loader: off** for this name | It rewrites scripts |

Download Cloudflare's origin-pull CA certificate (Cloudflare's documentation for Authenticated Origin Pulls links to it) and save it as `cloudflare-origin-pull-ca.pem`. The three files go on the server in step 4.

What Cloudflare changes for the application: the server never sees the visitor's address directly, so the proxy passes on the one Cloudflare reports; uploads are limited by Cloudflare's plan (100 MB on the free plan, above this application's 25 MB); API answers are marked "do not store", so Cloudflare does not cache them. Opt-out links and provider callbacks go through Cloudflare like everything else: **do not put a challenge or bot rule on `/api/v1/unsubscribe/*` or `/api/v1/webhooks/*`**, or mail clients and providers will be blocked.

## 2. AWS S3 — you

Pick one region (for example `eu-central-1`) and use it for everything below.

| Bucket | For | Settings |
|---|---|---|
| files, e.g. `seweb-crm-files` | Uploaded prospect lists and attachments | Block all public access; default encryption on; versioning on |
| backups, e.g. `seweb-crm-backups` | Encrypted database dumps | Block all public access; default encryption on; **lifecycle rule: expire objects after 30 days** |

The 30 days is the backup retention window stated in `docs/data-retention.md`. If you choose another number, change it there too.

Create **two IAM users with access keys**, each with only this policy (replace the bucket name):

Application — objects in the files bucket, nothing else:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::seweb-crm-files/*"}
  ]
}
```

Backup — may add to the backups bucket, and read for a restore; cannot delete:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": "arn:aws:s3:::seweb-crm-backups/*"},
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::seweb-crm-backups"}
  ]
}
```

The application never lists or creates buckets, so a wrong bucket name shows up as a failed upload, not at start-up.

## 3. AWS Cognito — you

1. Create a **user pool**. Sign-in identifier: email. **Self-registration: off** (you create the users). **MFA: required** (authenticator app). Required attribute: `email`.
2. Add a **domain** for the hosted sign-in page (a Cognito prefix domain is enough).
3. Create an **app client** of type *confidential* (it has a client secret):
   - Allowed callback URL: `https://crm.seweb.co/api/v1/auth/callback`
   - Grant: authorization code. Scopes: `openid`, `email`, `profile`.
4. Create the users (start with the owner's address). Each must have a **verified email**: the application refuses a sign-in whose `email_verified` is not true, and an invitation can only be accepted by the address it was sent to.
5. Note three values for step 5: the **issuer** `https://cognito-idp.<region>.amazonaws.com/<user pool id>`, the **client ID** and the **client secret**.

What to know about Cognito with this application:

- The application speaks standard OpenID Connect and needed no change for Cognito, but **it has only ever signed in against the local test provider. The first real sign-in is the test.**
- Cognito does not say in the sign-in token whether a second factor was used. The account page will therefore show "two-step verification: not reported". Enforcement is the user pool's MFA setting; keep it on *required*.
- Signing out of the application ends the application's session. It does not end the Cognito browser session, so "sign in" straight afterwards may not ask for the password again.

## 4. The server — you (I can give exact commands for each line)

1. A user for deployments, e.g. `deploy`, in the `docker` group. Docker Engine with the Compose plugin installed.
2. Directories: `/opt/seweb-crm` (owned by `deploy`), `/etc/seweb-crm` (mode 0700), `/var/backups/seweb-crm`, `/var/lib/seweb-crm`.
3. An SSH key pair for deployments: public half in `~deploy/.ssh/authorized_keys`, private half into GitHub (step 6).
4. Put the three Cloudflare files in `/etc/seweb-crm/edge/` (`origin.pem`, `origin.key` mode 0600, `cloudflare-origin-pull-ca.pem`), readable by the user Caddy runs as.
5. Add the site to the existing Caddy, which today belongs to the Hermes project: one line in its Caddyfile, `import /opt/seweb-crm/infra/production/Caddyfile.site`, then reload Caddy. That line is the only thing Hermes needs to know about the CRM; see "The proxy belongs to another project" below.
   - **Caddy installed on the host:** nothing else. The CRM is deployed with `compose.host-proxy.yaml` and Caddy forwards to `127.0.0.1:18000` and `127.0.0.1:18080`.
   - **Caddy in a container:** it cannot reach the host's loopback ports. Create a shared network once (`docker network create edge`), attach the Caddy container to it, mount `/etc/seweb-crm/edge` and `/opt/seweb-crm/infra/production/Caddyfile.site` read-only into it, and give it `CRM_API_UPSTREAM=crm-api:8000` and `CRM_FRONTEND_UPSTREAM=crm-frontend:8080`. The CRM is then deployed with `compose.shared-network.yaml` instead of `compose.host-proxy.yaml` (one line in `deploy.conf`).

## The proxy belongs to another project

Ports 80 and 443 on this server are held by a Caddy that is part of Hermes. Two projects cannot both own those ports, so one proxy has to serve both. That does **not** require moving anything today:

- **Now (smallest change):** Hermes keeps its Caddy and gains one `import` line. The CRM's routing, headers and certificate paths stay in this repository, in `Caddyfile.site`; the CRM's release never restarts or edits Hermes's Caddy.
- **Later, if you want them independent:** move the proxy into its own small repository ("edge") that owns ports 80 and 443 and nothing else, with one imported site file per project and the shared `edge` Docker network. Then deploying Hermes cannot take the CRM offline, and the other way round. Worth doing when a third thing arrives or when Hermes deployments start to restart the proxy; not a precondition for the first CRM deployment.

The one coupling that remains until then: if Hermes's Caddy is stopped or replaced, the CRM is unreachable too.

## 5. Settings files on the server — you type the secrets

All in `/etc/seweb-crm/`, mode 0600. Templates are in `infra/production/`.

| File | From template | Contains |
|---|---|---|
| `env` | `env.example` | Public address, database URL for the runtime role, session secret, encryption key, erasure key, monitoring token, Cognito issuer/client/secret, S3 region/bucket/keys |
| `db.env` | `db.env.example` | Database name and the three database passwords |
| `migrate.env` | `migrate.env.example` | The schema owner's database URL. Read only by the migration job |
| `deploy.conf` | `deploy.conf.example` | That the host's own proxy is used; the backup command with the backups bucket name |
| `backup.pass` | — | `openssl rand -base64 32 > backup.pass`. **Keep a copy somewhere else**: without it backups cannot be read |
| `backup-aws.env` | — | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` of the backup user |

Generate each secret with `openssl rand -base64 32`. `SECRET_ENCRYPTION_KEYS` has the form `v1:<that value>`. Every placeholder (`change-me…`) must be replaced: the application refuses to start with one left in. `ERASURE_HASH_KEY` is set once and never changed.

Keep these off for the first deployment: `EMAIL_DISPATCH=off`, `BILLING_MODE=off`, and the Gmail and Stripe settings empty.

## 6. GitHub — you

Repository → Settings → Environments → **production**:

| Kind | Name | Value |
|---|---|---|
| Variable | `DEPLOY_HOST` | `161.97.89.47` |
| Variable | `DEPLOY_USER` | `deploy` |
| Variable | `DEPLOY_HOST_KEY` | The server's public host key line, from `ssh-keyscan -t ed25519 161.97.89.47` (the part after the address). Compare it with the key shown on the server itself: `cat /etc/ssh/ssh_host_ed25519_key.pub` |
| Variable | `PUBLIC_BASE_URL` | `https://crm.seweb.co` |
| Variable | `ORIGIN_IP` | `161.97.89.47` — lets the smoke test confirm the server refuses connections that bypass Cloudflare |
| Secret | `DEPLOY_SSH_KEY` | The private half of the deployment key |

Also on that environment: **Required reviewers → yourself.** Without this rule, pushing a version tag deploys with nobody approving. And under Settings → Actions → General, workflow permissions can stay read-only; the release workflow asks for what it needs.

## 7. First deployment — me, when you say so

1. A tag `v0.1.0` on `main` starts the release workflow: checks → build and publish the two images → wait for your approval → deploy → smoke test from outside.
2. The deployment copies `infra/` to the server, logs the server in to the image registry for the pull only, takes an encrypted backup to S3, runs the database migrations as a one-off job, starts the services and waits for them to be healthy.
3. Then, by hand, once: sign in through Cognito as the owner, create the workspace, and confirm the page loads on a phone.

After that I rehearse a restore from the S3 backup and a rollback to the previous images, and record the results in `docs/test-evidence.md`.

## What can go wrong first time

- **Images are private.** The deployment logs in with the workflow's own token, so nothing needs setting; if the pull is refused, the package's access list in GitHub needs the repository added.
- **Cognito redirect mismatch**: the callback URL must match exactly, including `https` and no trailing slash.
- **Caddy cannot reach the loopback ports** if it runs in a container: see step 4.
- **Cloudflare shows error 525 or 526:** the origin certificate is missing, unreadable, or for another name, or the SSL mode is not "Full (strict)".
- **Cloudflare shows error 520/502 and Caddy logs a TLS handshake failure:** Authenticated Origin Pulls is off in Cloudflare while the server requires it (or the CA file is wrong).
- **A direct visit to `https://161.97.89.47` fails:** intended. Only Cloudflare can connect.
- **The first backup fails** if `backup-aws.env` or the bucket name in `deploy.conf` is wrong. The deployment then stops before changing anything, by design.
