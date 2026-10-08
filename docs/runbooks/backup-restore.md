# Runbook: local backup and restore

This covers the local and pilot setup. Scheduled, encrypted, off-host backups are part of phase 13.

## Back up

```bash
./infra/backup/backup.sh
```

Writes `backups/crm-<UTC timestamp>.dump` (PostgreSQL custom format) and checks that the file is a readable archive. The dump contains tenant data: keep it off shared drives and out of version control (`backups/` is git-ignored).

Uploaded files live in the object-storage volume (`storage-data`), not in the database. Back that volume up separately; the database only holds file metadata.

## Verify a backup

```bash
./infra/backup/restore-check.sh backups/crm-<stamp>.dump
```

Restores into a scratch database, compares every table's row count with the live database, confirms row-level security is still enabled on every tenant table and that policies were restored, then drops the scratch database. Run it straight after taking the backup: if the live database has changed since, the counts will differ and the check fails by design.

## Restore for real

1. Stop the API, worker and scheduler: `docker compose stop api worker scheduler`.
2. Create an empty database and prepare its roles and extensions:
   `docker compose exec db sh /docker-entrypoint-initdb.d/01-roles.sh <new-database>`
3. Restore: `docker compose exec -T db pg_restore --no-owner --no-comments --role=crm_migrator --dbname=<new-database> --username=<owner> --exit-on-error < backups/<file>`
4. Point `DATABASE_URL` and `MIGRATION_DATABASE_URL` at the new database, run `make migrate`, then start the services.
5. Sign in and check one tenant's lead count against what you expect. All sessions from before the backup that have since expired will ask users to sign in again.

## What a restore does not bring back

- Files uploaded after the storage volume was last backed up.
- Anything in Redis (pending sign-in attempts, queued jobs). Imports stuck in `parsing` or `committing` can be re-uploaded.
