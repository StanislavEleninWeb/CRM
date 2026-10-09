"""Operational figures across all workspaces, as counts only.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("send_intents", "mailboxes", "webhook_endpoints", "webhook_deliveries", "research_runs", "regulatory_sources")


def upgrade() -> None:
    # Monitoring needs totals over every workspace, and the tables force row security on their owner.
    # These policies let the owner read them only while the snapshot function is running; the
    # application role is not named in them, so it gains nothing.
    for table in TABLES:
        op.execute(
            f"CREATE POLICY {table}_ops_snapshot ON {table} FOR SELECT TO crm_migrator "
            "USING (current_setting('app.ops_snapshot', true) = 'on')"
        )
    op.execute(
        """
        CREATE FUNCTION ops_snapshot() RETURNS TABLE (name text, value double precision)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        BEGIN
            PERFORM set_config('app.ops_snapshot', 'on', true);
            RETURN QUERY
            SELECT 'due_jobs_due'::text, count(*)::double precision FROM due_jobs
                WHERE status IN ('pending', 'claimed') AND due_at <= now()
            UNION ALL SELECT 'due_jobs_oldest_due_seconds', COALESCE(EXTRACT(EPOCH FROM now() - min(due_at)), 0) FROM due_jobs
                WHERE status = 'pending' AND due_at <= now()
            UNION ALL SELECT 'due_jobs_failed', count(*) FROM due_jobs WHERE status = 'failed'
            UNION ALL SELECT 'sends_unknown', count(*) FROM send_intents WHERE state = 'unknown' AND resolved_at IS NULL
            UNION ALL SELECT 'sends_oldest_unknown_seconds', COALESCE(EXTRACT(EPOCH FROM now() - min(dispatched_at)), 0)
                FROM send_intents WHERE state = 'unknown' AND resolved_at IS NULL
            UNION ALL SELECT 'sends_stuck_dispatching', count(*) FROM send_intents
                WHERE state = 'dispatching' AND lease_expires_at < now() - interval '10 minutes'
            UNION ALL SELECT 'sends_waiting_overdue', count(*) FROM send_intents
                WHERE state = 'queued' AND scheduled_for < now() - interval '10 minutes'
            UNION ALL SELECT 'mailboxes_sync_stale', count(*) FROM mailboxes
                WHERE status IN ('active', 'degraded') AND (last_synced_at IS NULL OR last_synced_at < now() - interval '45 minutes')
            UNION ALL SELECT 'mailboxes_revoked', count(*) FROM mailboxes WHERE status = 'revoked' AND token_ciphertext IS NULL
                AND updated_at > now() - interval '7 days'
            UNION ALL SELECT 'mailbox_watches_expiring_24h', count(*) FROM mailboxes
                WHERE status IN ('active', 'degraded') AND watch_expires_at IS NOT NULL AND watch_expires_at < now() + interval '24 hours'
            UNION ALL SELECT 'webhook_endpoints_failing', count(*) FROM webhook_endpoints WHERE status = 'active' AND consecutive_failures >= 3
            UNION ALL SELECT 'webhook_deliveries_dead_24h', count(*) FROM webhook_deliveries
                WHERE status = 'dead' AND updated_at > now() - interval '24 hours'
            UNION ALL SELECT 'research_runs_paused', count(*) FROM research_runs WHERE status = 'paused'
            UNION ALL SELECT 'regulatory_sources_expired', count(*) FROM regulatory_sources
                WHERE superseded_at IS NULL AND expires_at < now();
            PERFORM set_config('app.ops_snapshot', 'off', true);
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION ops_snapshot() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION ops_snapshot() TO crm_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops_snapshot()")
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_ops_snapshot ON {table}")
