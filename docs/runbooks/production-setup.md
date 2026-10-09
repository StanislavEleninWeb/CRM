# Production setup: crm.seweb.co

Decided on 9 October 2026: one environment (production) on the VPS `161.97.89.47`, deployed by GitHub Actions, files and backups on AWS S3, sign-in through AWS Cognito, certificate notices to `management@seweb.co`.

**State: prepared. Nothing below has been done yet, and nothing has been deployed.** Each step says who does it. Secrets are typed on the server or into GitHub; they are never put in the repository or sent in chat.

Found by looking from outside, 9 October 2026:

- `crm.seweb.co` has **no DNS record** yet.
- The server **already runs Caddy on ports 80 and 443**. So this application does not start its own proxy: it listens on the server's loopback interface (`127.0.0.1:18000` for the API, `127.0.0.1:18080` for the pages) and the existing Caddy forwards to it.

## 1. DNS — you

Add an `A` record: `crm.seweb.co` → `161.97.89.47`. The certificate cannot be issued until this resolves.

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
4. Add the site to the existing Caddy: the block in `infra/production/Caddyfile.site`, then reload Caddy. **I need to know how that Caddy runs** (installed on the host, or in a container, and where its configuration file is) to say exactly where the block goes. If it runs in a container it must be able to reach the host's loopback ports, which changes the block slightly.

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
- **The first backup fails** if `backup-aws.env` or the bucket name in `deploy.conf` is wrong. The deployment then stops before changing anything, by design.
