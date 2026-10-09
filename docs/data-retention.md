# Data retention, erasure and deletion

What the application keeps, for how long, how something is removed, and what removal does not reach. Nothing here is a statement of legal compliance.

## Retention settings

Set per workspace under **Data and access**. A daily job in the database applies them.

| Record | Default | Allowed | What happens when it is older |
|---|---|---|---|
| Email text, HTML, preview and attachment names | 730 days | 30 – 3650 | The content is blanked. The line in the conversation (who, when, subject) stays |
| Security log | 730 days | 365 – 3650 | **Not yet purged by the job** — the setting is recorded; entries are currently kept until the workspace is deleted |
| Webhook delivery records | 30 days | 1 – 365 | Deleted (pending ones are kept) |
| Event records | 90 days | 7 – 365 | Deleted once no delivery refers to them |
| Finished scheduled jobs | 14 days | 1 – 90 | Deleted |
| Idempotency records | 24 hours | 24 – 168 | Deleted |
| Undecided research candidates | 30 days | fixed | Deleted (phase 07) |
| Ended support grants | 90 days after expiry | fixed | Deleted |

CRM records (companies, contacts, leads, opportunities, tasks, notes, call history) have no automatic expiry; they are removed by a person, by erasure, or with the workspace.

## Erasing one business on request

**Company → erase**, by an administrator or owner, with a reason and the name typed again.

Removed: the company, its contacts, channels, leads, assessments, observations, scores, notes, tasks, call history, activities, opportunities, files (rows and stored objects), email conversations and drafts linked to it or to its addresses, recipient classifications, consents and eligibility decisions for its addresses.

Kept, deliberately:

- **A do-not-email entry** for each of its email addresses, so it is not contacted again.
- **Tombstones**: keyed hashes of its email addresses, phone numbers, domain, list number and listing ID. They are per workspace and cannot be matched across workspaces. They exist so that a re-import, a research run or incoming mail does not recreate the record. The values themselves are not stored.
- **The audit entry** that an erasure happened, by whom, when and why. It contains no name or contact detail.

Effect afterwards: importing the same list skips that business (also under a new list number, if its website or address matches); a research candidate with its domain or listing cannot be promoted; mail from or to its addresses is not stored.

Not reached by erasure: backups, exports already downloaded, events already delivered to your webhooks, and the mailbox itself.

## Deleting a workspace

Owner only, with the workspace name typed. Nothing happens for seven days and the request can be cancelled. Then:

1. Mailbox authorisations and stored provider credentials are wiped, and API keys revoked.
2. The workspace row is deleted, and with it every record of the workspace, including its security log.
3. Its stored files are deleted from object storage.
4. One row remains in `tenant_deletions`: the workspace ID, when deletion was requested and when it happened.

A subscription is **not** cancelled by deletion; cancel it in billing first. Mailbox watches at Google are not explicitly stopped; they lapse within seven days and their notifications are ignored once the mailbox route is gone.

## Backups

`infra/backup/backup.sh` produces database dumps. A deleted or erased record remains inside dumps taken before the deletion until those dumps are removed.

- **The backup retention window has not been set.** It depends on the hosting target (U-07). Until it is, do not tell anyone how long backups persist. Phase 13 records the configured value here.
- Backups are restored only to recover from a failure. After a restore, erasures made since the dump must be re-applied; the tombstones do not survive a restore from before the erasure, so the audit log of the period and any erasure requests received must be replayed by hand. **This re-application is a manual procedure and is not automated.**
- No claim is made that data is erased from backups before they expire.

## Support access

Nobody outside a workspace can see it by default; there is no operator role in the application. The owner can allow one named, already-registered person read-only access for 1 to 72 hours with a stated reason.

- The person can read CRM records and reports. They cannot change anything, export, see the security log, billing, API keys, credentials, members, or — unless the owner ticked it — email conversations and drafts.
- Starting access writes `support.access_started` to the workspace's security log; granting and ending are logged too.
- Access ends at the stated time without anyone acting, or earlier when the owner ends it.
- It does not use a seat.

What this does not cover: people with direct database or server access at the hosting provider. That is a matter of operational access control and belongs to the hosting set-up (phase 13).
