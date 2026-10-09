# Production setup: crm.seweb.co

Decided on 9 and 10 October 2026: one environment (production) on the VPS `164.68.126.141`, deployed by GitHub Actions, behind Cloudflare, files and backups on AWS S3 and sign-in through AWS Cognito (both in `eu-central-1`, managed with Terraform), Apache on the server as the reverse proxy, certificate notices to `management@seweb.co`.

**State: prepared. Nothing below has been done yet, and nothing has been deployed.** Each step says who does it. Secrets are typed on the server or into GitHub; they are never put in the repository or sent in chat.

Found by looking from outside, 9 October 2026:

- `crm.seweb.co` has **no DNS record** yet.
- The server (`164.68.126.141`; an earlier address given was the Hermes server) **already runs Apache 2.4 on ports 80 and 443** for other projects, and an nginx answers on port 8080. Decided on 10 October: Apache stays, and the CRM is added to it as one more virtual host (step 3). The CRM listens on the loopback interface only.

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

Download Cloudflare's origin-pull CA certificate (Cloudflare's documentation for Authenticated Origin Pulls links to it) and save it as `cloudflare-origin-pull-ca.pem`. The three files go on the server in step 3, under `/etc/seweb-crm/edge/`.

What Cloudflare changes for the application: the server never sees the visitor's address directly, so the proxy passes on the one Cloudflare reports; uploads are limited by Cloudflare's plan (100 MB on the free plan, above this application's 25 MB); API answers are marked "do not store", so Cloudflare does not cache them. Opt-out links and provider callbacks go through Cloudflare like everything else: **do not put a challenge or bot rule on `/api/v1/unsubscribe/*` or `/api/v1/webhooks/*`**, or mail clients and providers will be blocked.

## 2. AWS: buckets, keys and sign-in — you, with Terraform

Everything in AWS is described in `infra/terraform/` (region `eu-central-1`): the files bucket, the backups bucket with its 30-day expiry, two IAM users that can each do one thing, and the Cognito user pool and app client. Follow `infra/terraform/README.md`:

1. Create the Terraform state bucket once, by hand (three commands in the README). The state holds the Cognito client secret, so it lives in that private bucket, not in the repository.
2. `terraform init`, `terraform plan`, read the plan, `terraform apply`.
3. `terraform output` gives the values for step 5 below.
4. Create one access key for each of the two IAM users **with the AWS CLI, not Terraform**, so the keys never enter the state.
5. Create the people who may sign in (one command per person in the README). Nobody can register themselves.

**The Terraform has been validated but never planned or applied.** The first `plan` is the real check.

What to know about Cognito with this application:

- The application speaks standard OpenID Connect and needed no change for Cognito, but **it has only ever signed in against the local test provider. The first real sign-in is the test.**
- Cognito does not say in the sign-in token whether a second factor was used. The account page will therefore show "two-step verification: not reported". Enforcement is the user pool's setting, which the Terraform sets to required.
- Signing out of the application ends the application's session. It does not end the Cognito browser session, so "sign in" straight afterwards may not ask for the password again.

## 3. Apache — you, once

The server already runs Apache for other projects, and it stays (decided 10 October). The CRM gets one more virtual host; nothing about the other sites changes.

1. Put the three Cloudflare files from step 1 in `/etc/seweb-crm/edge/`: `origin.pem`, `origin.key` (mode 0600, owner root) and `cloudflare-origin-pull-ca.pem`.
2. Install the virtual host:

   ```bash
   sudo cp /opt/seweb-crm/infra/production/apache-crm.seweb.co.conf /etc/apache2/sites-available/crm.seweb.co.conf
   ```

   ```bash
   sudo a2enmod ssl proxy proxy_http headers rewrite && sudo a2ensite crm.seweb.co
   ```

   ```bash
   sudo apache2ctl configtest
   ```

   Only if that says `Syntax OK`:

   ```bash
   sudo systemctl reload apache2
   ```

   A reload does not drop the other sites' connections. `a2enmod` only switches on modules that are off; if it enables `ssl` or `proxy` for the first time it asks for a restart instead of a reload, which interrupts all sites for a second.
3. Check that ports `18000` and `18080` are not used by anything else on the server: `sudo ss -ltnp | grep -E ':(18000|18080)\s'` should print nothing before the first deployment.

Until the CRM is deployed, the new site answers 503 through Cloudflare. That is expected.

What the virtual host does: redirects port 80 to HTTPS; serves the Cloudflare origin certificate and requires Cloudflare's client certificate; forwards `/api/`, `/healthz` and `/readyz` to the API on `127.0.0.1:18000` and everything else to the pages on `127.0.0.1:18080`; answers 404 for `/ops/`; refuses a request that declares more than 25 MB before forwarding any of it; passes on the visitor's address as Cloudflare reports it.

The separate proxy repository (<https://github.com/StanislavEleninWeb/caddy>) is **not used on this server**. It stays available for a future server, or for the day the other projects here move off Apache.

## 4. The server — you (I can give exact commands for each line)

1. A user for deployments, e.g. `deploy`, in the `docker` group. Docker Engine with the Compose plugin. The deployment never touches Apache.
2. Directories: `/opt/seweb-crm` (owned by `deploy`), `/etc/seweb-crm` (mode 0700), `/var/backups/seweb-crm`, `/var/lib/seweb-crm`.
3. An SSH key pair for deployments: public half in `~deploy/.ssh/authorized_keys`, private half into GitHub (step 6).

## 5. Settings files on the server — you type the secrets

All in `/etc/seweb-crm/`, mode 0600. Templates are in `infra/production/`.

| File | From template | Contains |
|---|---|---|
| `env` | `env.example` | Public address, database URL for the runtime role, session secret, encryption key, erasure key, monitoring token; from `terraform output`: Cognito issuer, client and secret, S3 region and bucket; the application user's access key |
| `db.env` | `db.env.example` | Database name and the three database passwords |
| `migrate.env` | `migrate.env.example` | The schema owner's database URL. Read only by the migration job |
| `deploy.conf` | `deploy.conf.example` | That Apache on the host is the proxy; the backup command with the backups bucket (`terraform output backups_uri`) |
| `backup.pass` | — | `openssl rand -base64 32 > backup.pass`. **Keep a copy somewhere else**: without it backups cannot be read |
| `backup-aws.env` | — | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` of the backup user |

Generate each secret with `openssl rand -base64 32`. `SECRET_ENCRYPTION_KEYS` has the form `v1:<that value>`. Every placeholder (`change-me…`) must be replaced: the application refuses to start with one left in. `ERASURE_HASH_KEY` is set once and never changed.

Keep these off for the first deployment: `EMAIL_DISPATCH=off`, `BILLING_MODE=off`, and the Gmail and Stripe settings empty.

## 6. GitHub — you

Repository → Settings → Environments → **production**:

| Kind | Name | Value |
|---|---|---|
| Variable | `DEPLOY_HOST` | `164.68.126.141` |
| Variable | `DEPLOY_USER` | `deploy` |
| Variable | `DEPLOY_HOST_KEY` | The server's public host key line, from `ssh-keyscan -t ed25519 164.68.126.141` (the part after the address). Compare it with the key shown on the server itself: `cat /etc/ssh/ssh_host_ed25519_key.pub` |
| Variable | `PUBLIC_BASE_URL` | `https://crm.seweb.co` |
| Variable | `ORIGIN_IP` | `164.68.126.141` — lets the smoke test confirm the server refuses connections that bypass Cloudflare |
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
- **Apache refuses to reload:** `apache2ctl configtest` names the line. The usual causes are a certificate file that is missing or unreadable, or a module that is not enabled.
- **Ports 18000 or 18080 are taken:** the deployment fails when starting the services. Set `API_HOST_PORT` and `FRONTEND_HOST_PORT` in `/etc/seweb-crm/deploy.conf` to free ports and change the same two numbers in the virtual host.
- **Cloudflare shows error 525 or 526:** the origin certificate is missing, unreadable, or for another name, or the SSL mode is not "Full (strict)".
- **Cloudflare shows error 520/502 and Apache logs a TLS handshake failure:** Authenticated Origin Pulls is off in Cloudflare while the server requires it (or the CA file is wrong).
- **A direct visit to `https://164.68.126.141` fails:** intended. Only Cloudflare can connect.
- **The first backup fails** if `backup-aws.env` or the bucket name in `deploy.conf` is wrong. The deployment then stops before changing anything, by design.
