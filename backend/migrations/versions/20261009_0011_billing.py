"""Plans, subscription state, billing customers and processed provider events.

Revision ID: 0011
Revises: 0010
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table = _base.tenant_table


def upgrade() -> None:
    # Plans are product configuration, the same for every tenant. No customer data lives here.
    op.execute(
        """
        CREATE TABLE plans (
            code text PRIMARY KEY CHECK (code ~ '^[a-z0-9_]{2,40}$'),
            name text NOT NULL,
            is_test boolean NOT NULL DEFAULT true,
            is_public boolean NOT NULL DEFAULT true,
            price_note text NOT NULL,
            provider_price_id text UNIQUE,
            seats_included integer NOT NULL CHECK (seats_included >= 1),
            research_runs_per_month integer NOT NULL CHECK (research_runs_per_month >= 0),
            emails_per_month integer NOT NULL CHECK (emails_per_month >= 0),
            api_keys integer NOT NULL CHECK (api_keys >= 0),
            webhook_endpoints integer NOT NULL CHECK (webhook_endpoints >= 0),
            platform_ai_allowance numeric(14, 4) NOT NULL DEFAULT 0 CHECK (platform_ai_allowance >= 0),
            allow_paid_overage boolean NOT NULL DEFAULT false,
            position integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("GRANT SELECT ON plans TO crm_app")
    # No price has been approved (U-08). These exist so the mechanics can be built and tested.
    op.execute(
        """
        INSERT INTO plans (code, name, is_test, is_public, price_note, seats_included, research_runs_per_month,
                           emails_per_month, api_keys, webhook_endpoints, position) VALUES
        ('trial', 'Trial', true, false, 'TEST PLAN - no price has been approved', 3, 5, 50, 1, 1, 0),
        ('test_starter', 'Starter (test)', true, true, 'TEST PRICE - not an approved price', 3, 20, 300, 2, 2, 1),
        ('test_team', 'Team (test)', true, true, 'TEST PRICE - not an approved price', 10, 100, 2000, 10, 10, 2)
        """
    )
    tenant_table(
        "tenant_billing",
        """
        plan_code text NOT NULL REFERENCES plans (code),
        status text NOT NULL CHECK (status IN ('trialing', 'active', 'past_due', 'canceled', 'unpaid', 'incomplete', 'unrecognized')),
        seats integer NOT NULL CHECK (seats >= 1),
        trial_ends_at timestamptz,
        current_period_end timestamptz,
        cancel_at_period_end boolean NOT NULL DEFAULT false,
        canceled_at timestamptz,
        past_due_since timestamptz,
        provider_subscription_id text,
        provider_price_id text,
        synced_at timestamptz,
        sync_error text,
        CONSTRAINT uq_tenant_billing_one UNIQUE (tenant_id)
        """,
    )
    # A provider callback names a customer, not a tenant. This is the only mapping that is trusted,
    # and it is read through a definer function, so the table uses ENABLE (as for mailbox routes).
    op.execute(
        """
        CREATE TABLE billing_customers (
            tenant_id uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
            provider_customer_id text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE billing_customers ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY billing_customers_tenant ON billing_customers FOR ALL TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE FUNCTION billing_customer_tenant(p_customer text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT c.tenant_id FROM billing_customers c WHERE c.provider_customer_id = p_customer $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION billing_customer_tenant(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION billing_customer_tenant(text) TO crm_app")
    # One row per provider event, whatever tenant it turns out to concern: the duplicate check.
    op.execute(
        """
        CREATE TABLE billing_events (
            provider_event_id text PRIMARY KEY,
            event_type text NOT NULL,
            provider_customer_id text,
            received_at timestamptz NOT NULL DEFAULT now(),
            processed_at timestamptz,
            outcome text
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON billing_events TO crm_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing_events")
    op.execute("DROP FUNCTION IF EXISTS billing_customer_tenant(text)")
    op.execute("DROP TABLE IF EXISTS billing_customers")
    op.execute("DROP TABLE IF EXISTS tenant_billing CASCADE")
    op.execute("DROP TABLE IF EXISTS plans")
