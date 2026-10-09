# Production setup: crm.seweb.co

Decided on 9 and 10 October 2026: one environment (production) on the VPS `161.97.89.47`, deployed by GitHub Actions, behind Cloudflare, files and backups on AWS S3 and sign-in through AWS Cognito (both in `eu-central-1`, managed with Terraform), a shared reverse proxy in its own repository, certificate notices to `management@seweb.co`.

**State: prepared. Nothing below has been done yet, and nothing has been deployed.** Each step says who does it. Secrets are typed on the server or into GitHub; they are never put in the repository or sent in chat.

Found by looking from outside, 9 October 2026:

- `crm.seweb.co` has **no DNS record** yet.
- The server **already runs Caddy on ports 80 and 443**, in a container that is part of the Hermes stack. Decided on 10 October: that proxy moves into its own repository and serves every project (step 3). The CRM publishes no ports; it joins the proxy's Docker network.

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

Download Cloudflare's origin-pull CA certificate (Cloudflare's documentation for Authenticated Origin Pulls links to it) and save it as `cloudflare-origin-pull-ca.pem`. The three files go on the server in step 4, under `/etc/edge/certs/crm/`.

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

## 3. The shared proxy — you, once per server

Ports 80 and 443 on the server are currently held by the Caddy inside the Hermes stack. They move to a proxy of their own, in its own repository: <https://github.com/StanislavEleninWeb/caddy>. Each project then joins the shared Docker network `edge` and installs one site file; deploying a project never restarts the proxy, and a broken site file is refused instead of taking the others down.

- Installing the proxy and taking the ports over from Hermes is described in that repository (`README.md`, `docs/migrating-hermes.md`). **It has not been done, and it needs a small change in the Hermes repository** (the agents join the `edge` network, the `caddy` service is removed, the site block moves to a site file). The switch-over interrupts the agent dashboards for about a minute.
- The CRM cannot go live before this is done: it publishes no ports of its own.

## 4. The server — you (I can give exact commands for each line)

1. A user for deployments, e.g. `deploy`, in the `docker` group, allowed to write `/etc/edge/sites` (the site installer runs as this user). Docker Engine with the Compose plugin.
2. Directories: `/opt/seweb-crm` (owned by `deploy`), `/etc/seweb-crm` (mode 0700), `/var/backups/seweb-crm`, `/var/lib/seweb-crm`.
3. An SSH key pair for deployments: public half in `~deploy/.ssh/authorized_keys`, private half into GitHub (step 6).
4. The three Cloudflare files from step 1 in `/etc/edge/certs/crm/`: `origin.pem`, `origin.key`, `cloudflare-origin-pull-ca.pem`. The proxy container reads them as root; keep the key mode 0600.

The CRM's deployment then does the rest by itself: it joins the `edge` network and runs `edge-site install crm …` with the site file from this repository.

## 5. Settings files on the server — you type the secrets

All in `/etc/seweb-crm/`, mode 0600. Templates are in `infra/production/`.

| File | From template | Contains |
|---|---|---|
| `env` | `env.example` | Public address, database URL for the runtime role, session secret, encryption key, erasure key, monitoring token; from `terraform output`: Cognito issuer, client and secret, S3 region and bucket; the application user's access key |
| `db.env` | `db.env.example` | Database name and the three database passwords |
| `migrate.env` | `migrate.env.example` | The schema owner's database URL. Read only by the migration job |
| `deploy.conf` | `deploy.conf.example` | That the shared proxy is used; the backup command with the backups bucket (`terraform output backups_uri`) |
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
- **The deployment stops at "Install this application's site file"**: the shared proxy is not installed yet (step 3), or the three certificate files are missing from `/etc/edge/certs/crm/`. The application itself is already running at that point; only its public route is missing. Fix the cause and run the deployment again.
- **Cloudflare shows error 525 or 526:** the origin certificate is missing, unreadable, or for another name, or the SSL mode is not "Full (strict)".
- **Cloudflare shows error 520/502 and Caddy logs a TLS handshake failure:** Authenticated Origin Pulls is off in Cloudflare while the server requires it (or the CA file is wrong).
- **A direct visit to `https://161.97.89.47` fails:** intended. Only Cloudflare can connect.
- **The first backup fails** if `backup-aws.env` or the bucket name in `deploy.conf` is wrong. The deployment then stops before changing anything, by design.
