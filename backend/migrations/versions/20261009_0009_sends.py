"""Reliable manual sends: approval review record, send limits, reconciliation fields and the event outbox.

Revision ID: 0009
Revises: 0008
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table = _base.tenant_table

STATES = "'queued', 'claimed', 'dispatching', 'provider_accepted', 'failed', 'cancelled', 'unknown', 'blocked'"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE send_intents
            ADD COLUMN reconcile_attempts integer NOT NULL DEFAULT 0,
            ADD COLUMN resolved_by uuid,
            ADD COLUMN resolved_at timestamptz,
            ADD COLUMN resolution_note text,
            ADD COLUMN cancelled_by uuid
        """
    )
    # 'simulated' ends a dry run: every step ran except the provider call.
    op.execute("ALTER TABLE send_intents DROP CONSTRAINT send_intents_state_check")
    op.execute(
        f"ALTER TABLE send_intents ADD CONSTRAINT send_intents_state_check CHECK (state IN ({STATES}, 'simulated'))"
    )
    op.execute("DROP INDEX ix_send_intents_review")
    op.execute(
        "CREATE INDEX ix_send_intents_open ON send_intents (tenant_id, state) "
        "WHERE state IN ('queued', 'claimed', 'dispatching', 'unknown', 'failed', 'blocked')"
    )
    op.execute(
        "CREATE INDEX ix_send_intents_mailbox_sent ON send_intents (tenant_id, mailbox_id, dispatched_at) WHERE dispatched_at IS NOT NULL"
    )
    # A person who approves a message that needs review says why; the reasons they saw are kept.
    op.execute(
        "ALTER TABLE email_drafts ADD COLUMN review_note text, ADD COLUMN reviewed_codes text[] NOT NULL DEFAULT '{}'"
    )
    # Conservative product defaults, not the provider's own ceiling.
    op.execute(
        """
        ALTER TABLE mailboxes
            ADD COLUMN daily_send_limit integer NOT NULL DEFAULT 50 CHECK (daily_send_limit BETWEEN 1 AND 2000),
            ADD COLUMN min_send_interval_seconds integer NOT NULL DEFAULT 30 CHECK (min_send_interval_seconds BETWEEN 0 AND 3600)
        """
    )
    # Events written in the same transaction as the change they describe. Delivery arrives with webhooks.
    tenant_table(
        "outbox_events",
        """
        event_type text NOT NULL,
        subject_type text NOT NULL,
        subject_id uuid,
        payload jsonb NOT NULL DEFAULT '{}'::jsonb
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_outbox_events_recent ON outbox_events (tenant_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbox_events CASCADE")
    op.execute("ALTER TABLE mailboxes DROP COLUMN daily_send_limit, DROP COLUMN min_send_interval_seconds")
    op.execute("ALTER TABLE email_drafts DROP COLUMN review_note, DROP COLUMN reviewed_codes")
    op.execute("DROP INDEX IF EXISTS ix_send_intents_mailbox_sent")
    op.execute("DROP INDEX IF EXISTS ix_send_intents_open")
    op.execute("UPDATE send_intents SET state = 'cancelled' WHERE state = 'simulated'")
    op.execute("ALTER TABLE send_intents DROP CONSTRAINT send_intents_state_check")
    op.execute(f"ALTER TABLE send_intents ADD CONSTRAINT send_intents_state_check CHECK (state IN ({STATES}))")
    op.execute(
        "CREATE INDEX ix_send_intents_review ON send_intents (tenant_id, state) WHERE state IN ('unknown', 'failed', 'blocked')"
    )
    op.execute(
        """
        ALTER TABLE send_intents
            DROP COLUMN reconcile_attempts, DROP COLUMN resolved_by, DROP COLUMN resolved_at,
            DROP COLUMN resolution_note, DROP COLUMN cancelled_by
        """
    )
