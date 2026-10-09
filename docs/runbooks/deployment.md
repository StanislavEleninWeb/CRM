# Deployment, migrations and rollback

**State: prepared and validated locally. Nothing has been deployed; there is no hosting target (U-07).** What "validated locally" covered is listed in `docs/test-evidence.md` under phase 13.

## What exists

| Piece | Where |
|---|---|
| Production images (API/worker/scheduler/migrations share one; frontend is static files behind nginx) | `backend/Dockerfile`, `frontend/Dockerfile`, target `prod` |
| Stack for one host | `infra/production/compose.yaml` |
| TLS reverse proxy | `infra/production/Caddyfile` (profile `proxy`) |
| Inventory of settings | `infra/production/env.example`, `db.env.example` — placeholders, refused at start-up |
| Deployment | `infra/deploy/deploy.sh` on the host; `remote.sh` from the pipeline |
| Pipeline | `.github/workflows/release.yml` |
| Checks | `infra/checks/image-checks.sh`, `smoke-remote.sh`, `upgrade-from-previous.sh` |

## Before the first deployment (owner and operator)

1. **Look at the host first.** `ss -ltnp | grep -E ':(80|443|5432|6379)\b'` and `docker ps`. If something already listens on 80/443, do not start the bundled proxy: set `COMPOSE_PROFILES_ARGS=""` and `START_PROXY=""` for `deploy.sh`, attach the existing proxy to the `edge` network, and route `/api/*`, `/healthz`, `/readyz` to `api:8000` and everything else to `frontend:8080`. Keep `/ops/*` unreachable from outside. Do not change services that are not this application's.
2. Put the real settings at `/etc/seweb-crm/env`, `/etc/seweb-crm/db.env` and `/etc/seweb-crm/migrate.env`, mode 0600, owned by the deploying user. Generate secrets on the host: `openssl rand -base64 32`. The schema owner's connection string goes only in `migrate.env`: the API, worker and scheduler never receive it, so a compromised application process is still confined to the role that cannot bypass row-level security. Operator commands that need it (key rotation, attaching a price to a plan) are run through the migration job: `docker compose -f infra/production/compose.yaml --profile release run --rm migrate python -m app.modules.providers.rotate`.
3. Check out the repository's `infra/` at `/opt/seweb-crm`.
4. In GitHub: create environments `staging` and `production`; set variables `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_HOST_KEY` (the host's public key line), `PUBLIC_BASE_URL`, and the secret `DEPLOY_SSH_KEY`. **Add required reviewers to `production`.** That rule lives in repository settings; without it the production job runs as soon as staging passes.
5. Make the registry packages readable by the host (`docker login ghcr.io` with a read-only token).

## What a release does

`checks` (the CI workflow) → `images` (build once, run the image checks, push under the commit SHA, output digests) → `staging` (deploy those digests, smoke test) → `production` (the same digests, after approval). Staging and production deployments are each serialised, so two releases cannot interleave.

On the host, `deploy.sh`:

1. validates the Compose configuration and refuses images that are not pinned by digest;
2. pulls;
3. takes a backup with `BACKUP_COMMAND` — it refuses to migrate without one unless `SKIP_BACKUP=1` is given;
4. runs `alembic upgrade head` as a **one-off job** with the migration role;
5. replaces the services and waits for their health checks;
6. records the release, keeping the previous one in `previous-release.env`.

A failed migration or health check stops the script with a non-zero exit, which fails the pipeline job; production is never offered a digest that did not pass staging. No API replica runs migrations when it starts.

## Migrations: expand, then contract

The API, worker and scheduler are replaced after the migration, so for a short time **old code runs against the new schema**. And after a rollback, old code runs against it for longer. So:

- **Expand** in one release: add tables, nullable columns, new functions; widen a constraint. Old code must keep working.
- **Contract** in a later release, once no running code needs the old shape: drop columns, tighten constraints, remove functions.
- A migration that old code cannot tolerate (renaming a column in place, narrowing a check that old code violates) must be split, or the release needs a maintenance window. Say so in the release notes.
- Every migration has a `downgrade`. The round trip up → base → up is run in CI; downgrading a production database loses whatever the dropped structures held and is a last resort, not the rollback path.

## Rollback

Rolling back means deploying the previous image digests again:

```bash
. /var/lib/seweb-crm/previous-release.env && SKIP_MIGRATIONS=1 API_IMAGE="$API_IMAGE" FRONTEND_IMAGE="$FRONTEND_IMAGE" /opt/seweb-crm/infra/deploy/deploy.sh
```

`SKIP_MIGRATIONS=1` matters: the earlier image's migration tool does not know the newer schema revision and would stop the deployment. The schema stays as it is and the earlier code runs against it, which is safe only for releases that followed the expand rule. If the earlier code cannot tolerate the newer schema, restore the pre-migration backup instead (`backup-restore.md`) and accept losing what was written since.

What was demonstrated locally is in the test evidence; a rollback on a real host has not been rehearsed.

## Scaling notes

- Exactly one `scheduler`. More than one only causes more polling; the database claim still hands each due row to one worker.
- `worker` can be scaled; sends and deliveries are safe under concurrent workers.
- `api` runs two processes; add replicas behind the proxy if needed. Sessions are in the database, not in memory.
- Redis holds no schedule and nothing durable. If it is lost, due rows are simply claimed again after their lease.
