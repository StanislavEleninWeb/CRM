"""Prospect workspace: call attempts, verification, tenant queue settings.

A dialer launch and a reported call are different facts. ``launched_at`` records that
the user opened the dialer; ``outcome`` is only ever set by an explicit user report.

Revision ID: 0005
Revises: 0004
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table, tenant_fk, member_fk = _base.tenant_table, _base.tenant_fk, _base.member_fk

OUTCOMES = "('no_answer', 'busy', 'voicemail', 'connected', 'follow_up_requested', 'wrong_number', 'not_interested')"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenants ADD COLUMN shortlist_size integer NOT NULL DEFAULT 25 CHECK (shortlist_size BETWEEN 1 AND 500)"
    )
    op.execute(
        "ALTER TABLE tenants ADD COLUMN evidence_freshness_days integer NOT NULL DEFAULT 30 "
        "CHECK (evidence_freshness_days BETWEEN 1 AND 3650)"
    )

    tenant_table(
        "call_attempts",
        f"""
        lead_id uuid,
        company_id uuid NOT NULL,
        contact_id uuid,
        channel_id uuid,
        dialed_value text NOT NULL,
        launched_at timestamptz,
        outcome text CHECK (outcome IN {OUTCOMES}),
        outcome_reported_at timestamptz,
        outcome_source text NOT NULL DEFAULT 'user_reported' CHECK (outcome_source IN ('user_reported', 'provider_confirmed')),
        notes text,
        follow_up_task_id uuid,
        follow_up_requested_via text,
        follow_up_scope text,
        requested_email text,
        created_by uuid,
        {tenant_fk("call_attempts", "lead_id", "leads", "SET NULL")},
        {tenant_fk("call_attempts", "company_id", "companies")},
        {tenant_fk("call_attempts", "contact_id", "contacts", "SET NULL")},
        {tenant_fk("call_attempts", "channel_id", "contact_channels", "SET NULL")},
        {tenant_fk("call_attempts", "follow_up_task_id", "tasks", "SET NULL")},
        {member_fk("call_attempts", "created_by")},
        CONSTRAINT ck_call_attempts_outcome_time CHECK ((outcome IS NULL) = (outcome_reported_at IS NULL)),
        CONSTRAINT ck_call_attempts_something_happened CHECK (launched_at IS NOT NULL OR outcome IS NOT NULL)
        """,
    )
    op.execute("CREATE INDEX ix_call_attempts_lead ON call_attempts (tenant_id, lead_id, created_at DESC)")
    op.execute("CREATE INDEX ix_call_attempts_company ON call_attempts (tenant_id, company_id, created_at DESC)")
    op.execute("REVOKE DELETE, TRUNCATE ON call_attempts FROM crm_app")

    # Fields a reviewer edited by hand are carried forward when research is re-imported.
    op.execute("ALTER TABLE lead_assessments ADD COLUMN user_edited_fields text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE lead_assessments ADD COLUMN verified_by uuid")
    op.execute("ALTER TABLE lead_assessments ADD COLUMN verified_at timestamptz")
    op.execute("ALTER TABLE observations ADD COLUMN verification_note text")
    op.execute("ALTER TABLE hypotheses ADD COLUMN resolution_note text")

    op.execute("CREATE INDEX ix_tasks_lead_open ON tasks (tenant_id, lead_id, due_at) WHERE status = 'open'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tasks_lead_open")
    op.execute("ALTER TABLE hypotheses DROP COLUMN IF EXISTS resolution_note")
    op.execute("ALTER TABLE observations DROP COLUMN IF EXISTS verification_note")
    for column in ("verified_at", "verified_by", "user_edited_fields"):
        op.execute(f"ALTER TABLE lead_assessments DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS call_attempts CASCADE")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS evidence_freshness_days")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS shortlist_size")
