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

## Encrypted backups for a hosted environment

```bash
BACKUP_PASSPHRASE_FILE=/etc/seweb-crm/backup.pass BACKUP_RETENTION_DAYS=30 \
  COMPOSE="docker compose -f /opt/seweb-crm/infra/production/compose.yaml" \
  /opt/seweb-crm/infra/backup/backup-encrypted.sh /var/backups/seweb-crm
```

- Each run writes `crm-<UTC time>.dump.enc` and a checksum, proves the file decrypts to the dump it was made from, and deletes backups older than the retention window.
- **Retention window: 30 days by default.** This is how long a deleted or erased record can still be recovered from a backup. If you change `BACKUP_RETENTION_DAYS`, change the statement in `docs/data-retention.md` too.
- Keep the passphrase somewhere other than the backups. Copy backups off the host; a backup on the same disk as the database protects against mistakes, not against losing the host. Where they are copied to is undecided (U-07).
- Schedule it daily, for example with a systemd timer or cron: `15 2 * * * <the command above> >> /var/log/seweb-crm-backup.log 2>&1`. Alert if the newest backup is older than 26 hours.
- Object storage (uploaded lists and attached files) is **not** in the database dump. Use the storage provider's versioning or replication.
- Not backed up on purpose: Redis (nothing durable), and the settings files with secrets (keep those in a secret store).

## Checking a backup restores

```bash
BACKUP_PASSPHRASE_FILE=/etc/seweb-crm/backup.pass ./infra/backup/restore-check.sh /var/backups/seweb-crm/crm-<stamp>.dump.enc
```

It verifies the checksum, decrypts to a temporary file, restores into a scratch database, compares row counts with the live database table by table, checks that row-level security and its policies came back, and drops the scratch database. Run it after every schema change and at least monthly.

## Restoring for real

1. Stop `api`, `worker` and `scheduler`. Keep `db` running.
2. Take a backup of the current state, even if it is damaged.
3. Create an empty database with `infra/postgres/init/01-roles.sh <name>`, restore into it with `pg_restore --no-owner --role=crm_migrator`, check it, then point the application at it (or rename).
4. Start the services with the image that matches the backup's schema revision, or run the migration job if the images are newer.
5. **Re-apply erasures.** A backup taken before a business was erased brings it back, together with the loss of its tombstone. List erasures since the backup time from wherever erasure requests are recorded outside the database (the request emails, the ticket) and erase each again. The audit log cannot tell you: it was restored to the same earlier point. **Keep a record of erasure requests outside the database for at least the backup retention window.**
6. Re-apply opt-outs the same way: suppressions added after the backup are gone. Unsubscribe links clicked in that period must be honoured again from the mail server logs or by asking recipients; until that is done, keep `EMAIL_DISPATCH=off`.
7. Mailboxes resynchronise by themselves. Emails sent after the backup time are found again from the mailbox; nothing is resent.
8. Workspaces deleted after the backup time come back. Delete them again from the `tenant_deletions` record you kept (it is in the backup only up to the backup time, so compare with the pre-restore copy from step 2).
