# Data retention, erasure and deletion

What the application keeps, for how long, how something is removed, and what removal does not reach. Nothing here is a statement of legal compliance.

## Retention settings

Set per workspace under **Data and access**. A daily job in the database applies them.

| Record | Default | Allowed | What happens when it is older |
|---|---|---|---|
| Email text, HTML, preview and attachment names | 730 days | 30 – 3650 | The content is blanked. The line in the conversation (who, when, subject) stays |
| Security log | 730 days | 365 – 3650 | Deleted. The application cannot delete from the log directly or set this below a year; the purge is a database function limited to the current workspace |
| Webhook delivery records | 30 days | 1 – 365 | Deleted (pending ones are kept) |
| Event records | 90 days | 7 – 365 | Deleted once no delivery refers to them |
| Finished scheduled jobs | 14 days | 1 – 90 | Deleted |
| Idempotency records | 24 hours | 24 – 168 | Deleted |
| Undecided research candidates | 30 days | fixed | Deleted (phase 07) |
| Ended support grants | 90 days after expiry | fixed | Deleted |

CRM records (companies, contacts, leads, opportunities, tasks, notes, call history) have no automatic expiry; they are removed by a person, by erasure, or with the workspace.

## Erasing one business on request

**Company → erase**, by an administrator or owner, with a reason and the name typed again.

Refused while an email to the business is being sent or its outcome is unknown: settle it first, so the record a person needs is not lost. An email still waiting is cancelled.

Removed: the company, its contacts, channels, leads, assessments, observations, scores, notes, tasks, call history, activities, opportunities, files (rows and stored objects), email conversations and drafts linked to it or to its addresses, recipient classifications, consents and eligibility decisions for its addresses; its rows in past imports and the **original uploaded files of every list that contained it** (the whole file, since it cannot be edited); and research candidates with its domain or listing.

Kept, deliberately:

- **A do-not-email entry** for each of its email addresses, so it is not contacted again.
- **Tombstones**: keyed hashes of its email addresses, phone numbers, domain, list number and listing ID. They are per workspace and cannot be matched across workspaces. They exist so that a re-import, a research run or incoming mail does not recreate the record. The values themselves are not stored.
- **The audit entry** that an erasure happened, by whom, when and why. It contains no name or contact detail.

Effect afterwards: importing the same list skips that business (also under a new list number or name, if its website or email address matches); a research run does not store it as a candidate and it cannot be promoted; mail from or to its addresses is not stored.

The hashes are keyed with `ERASURE_HASH_KEY`. A hosted environment refuses to start without it; `make up` generates one locally (in development an empty value falls back to the session secret). **Changing that key silently un-erases every business**: set it once, back it up with the encryption keys, and do not rotate it without recreating the tombstones.

Not reached by erasure: backups, exports already downloaded, events already delivered to your webhooks, and the mailbox itself.

## Deleting a workspace

Owner only, with the workspace name typed. Nothing happens for seven days and the request can be cancelled. Then:

1. For each mailbox, Gmail notifications are stopped and the authorisation is revoked at Google (best effort; a failure is logged). Stored provider credentials are wiped and API keys revoked.
2. The workspace row is deleted, and with it every record of the workspace, including its security log.
3. Its stored files are deleted from object storage.
4. One row remains in `tenant_deletions`: the workspace ID, when deletion was requested and when it happened.

Deletion cannot be scheduled while a subscription is running, so a deleted workspace is not charged. Cancel in billing first.

## Backups

`infra/backup/backup.sh` produces database dumps. A deleted or erased record remains inside dumps taken before the deletion until those dumps are removed.

- **Backup retention window: 30 days by default** (`BACKUP_RETENTION_DAYS` in `infra/backup/backup-encrypted.sh`). A deleted or erased record can be recovered from a backup for that long, and not after. This is the configured default of the script; no hosted environment exists yet, so no backup has been taken under it.
- Backups are restored only to recover from a failure. A restore from before an erasure brings the record back **and loses its tombstone and its audit entry**, so erasures and opt-outs made since the backup must be re-applied by hand from a record kept outside the database. The steps are in `docs/runbooks/backup-restore.md`. This is not automated.
- No claim is made that data is erased from backups before they expire.

## Support access

Nobody outside a workspace can see it by default; there is no operator role in the application. The owner can allow one named, already-registered person read-only access for 1 to 72 hours with a stated reason.

- The person can read CRM records and reports. They cannot change anything, export, see the security log, billing, API keys, credentials, members, or — unless the owner ticked it — email conversations and drafts.
- Starting access writes `support.access_started` to the workspace's security log; granting and ending are logged too.
- Access ends at the stated time without anyone acting, or earlier when the owner ends it.
- It does not use a seat.

What this does not cover: people with direct database or server access at the hosting provider. That is a matter of operational access control and belongs to the hosting set-up (phase 13).
