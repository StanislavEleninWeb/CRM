"""Provider connections, usage ledger and budgets.

Secrets are stored only as AES-GCM ciphertext with the key version beside them; the key
material lives outside the database. Budget reservations are taken with one conditional
update so concurrent jobs cannot oversubscribe a budget.

Revision ID: 0006
Revises: 0005
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table, tenant_fk, member_fk = _base.tenant_table, _base.tenant_fk, _base.member_fk

TABLES = ("provider_connections", "budgets", "budget_reservations", "usage_ledger")


def upgrade() -> None:
    tenant_table(
        "provider_connections",
        f"""
        provider text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{{1,39}}$'),
        purpose text NOT NULL CHECK (purpose IN ('model', 'discovery', 'mailbox', 'telephony', 'other')),
        label text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),
        access_mode text NOT NULL CHECK (access_mode IN ('byok', 'platform', 'oauth')),
        status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'active', 'error', 'revoked')),
        secret_ciphertext bytea,
        secret_nonce bytea,
        secret_key_version text,
        secret_hint text,
        config jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        verification text NOT NULL DEFAULT 'implemented'
            CHECK (verification IN ('implemented', 'verified_locally', 'verified_in_sandbox', 'verified_live')),
        last_checked_at timestamptz,
        last_ok_at timestamptz,
        last_error text,
        rate_limited_until timestamptz,
        consecutive_failures integer NOT NULL DEFAULT 0,
        revoked_at timestamptz,
        created_by uuid,
        {member_fk("provider_connections", "created_by")},
        CONSTRAINT ck_provider_secret_complete CHECK (
            (secret_ciphertext IS NULL AND secret_nonce IS NULL AND secret_key_version IS NULL)
            OR (secret_ciphertext IS NOT NULL AND secret_nonce IS NOT NULL AND secret_key_version IS NOT NULL)
        )
        """,
    )
    op.execute("CREATE INDEX ix_provider_connections_purpose ON provider_connections (tenant_id, purpose, status)")

    tenant_table(
        "budgets",
        """
        scope text NOT NULL CHECK (scope IN ('research', 'model', 'discovery', 'all')),
        period text NOT NULL CHECK (period IN ('month', 'total')),
        period_start date NOT NULL,
        currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
        limit_amount numeric(14, 4) NOT NULL CHECK (limit_amount >= 0),
        spent_amount numeric(14, 4) NOT NULL DEFAULT 0 CHECK (spent_amount >= 0),
        reserved_amount numeric(14, 4) NOT NULL DEFAULT 0 CHECK (reserved_amount >= 0),
        max_concurrent_runs integer NOT NULL DEFAULT 1 CHECK (max_concurrent_runs BETWEEN 1 AND 50),
        allow_overage boolean NOT NULL DEFAULT false,
        -- Set when a provider's actual charge turned out higher than what was reserved.
        overrun boolean NOT NULL DEFAULT false,
        CONSTRAINT uq_budgets_scope_period UNIQUE (tenant_id, scope, period, period_start),
        -- The invariant: what is spent plus what is held never exceeds the limit, unless overage was
        -- explicitly allowed or a real charge has already exceeded it (which blocks further work).
        CONSTRAINT ck_budgets_not_oversubscribed CHECK (
            allow_overage OR overrun OR spent_amount + reserved_amount <= limit_amount
        )
        """,
    )

    tenant_table(
        "budget_reservations",
        f"""
        budget_id uuid NOT NULL,
        connection_id uuid,
        idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 3 AND 200),
        purpose text NOT NULL,
        run_ref text,
        state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved', 'settled', 'released', 'unknown')),
        reserved_amount numeric(14, 4) NOT NULL CHECK (reserved_amount >= 0),
        actual_amount numeric(14, 4),
        currency char(3) NOT NULL,
        note text,
        created_by uuid,
        settled_at timestamptz,
        {tenant_fk("budget_reservations", "budget_id", "budgets", "RESTRICT")},
        {tenant_fk("budget_reservations", "connection_id", "provider_connections", "SET NULL")},
        CONSTRAINT uq_budget_reservations_key UNIQUE (tenant_id, idempotency_key)
        """,
    )
    op.execute(
        "CREATE INDEX ix_budget_reservations_open ON budget_reservations (tenant_id, budget_id) WHERE state IN ('reserved', 'unknown')"
    )

    tenant_table(
        "usage_ledger",
        f"""
        occurred_at timestamptz NOT NULL DEFAULT now(),
        connection_id uuid,
        reservation_id uuid,
        provider text NOT NULL,
        purpose text NOT NULL,
        run_ref text,
        units jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        amount numeric(14, 4) NOT NULL DEFAULT 0 CHECK (amount >= 0),
        currency char(3) NOT NULL,
        cost_basis text NOT NULL CHECK (cost_basis IN ('estimated', 'provider_reported', 'verified_invoice', 'unknown')),
        pricing_version text,
        reconciliation text NOT NULL DEFAULT 'pending' CHECK (reconciliation IN ('pending', 'reconciled', 'disputed')),
        billed_to text NOT NULL CHECK (billed_to IN ('tenant_provider_account', 'platform')),
        note text,
        {tenant_fk("usage_ledger", "connection_id", "provider_connections", "SET NULL")},
        {tenant_fk("usage_ledger", "reservation_id", "budget_reservations", "SET NULL")}
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_usage_ledger_time ON usage_ledger (tenant_id, occurred_at DESC)")
    # The ledger is append-only. Corrections are new entries; only reconciliation status may change.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON usage_ledger FROM crm_app")
    op.execute("GRANT UPDATE (reconciliation, note) ON usage_ledger TO crm_app")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
