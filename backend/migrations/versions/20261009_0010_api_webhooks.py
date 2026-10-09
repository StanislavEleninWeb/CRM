"""API keys, idempotency records, webhook endpoints and deliveries.

Revision ID: 0010
Revises: 0009
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table, tenant_fk, member_fk = _base.tenant_table, _base.tenant_fk, _base.member_fk


def upgrade() -> None:
    # A key is presented before any tenant is known, so it is looked up through a definer
    # function; that is why this table uses ENABLE, not FORCE (as for memberships).
    op.execute(
        f"""
        CREATE TABLE api_keys (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
            prefix text NOT NULL,
            key_hash bytea NOT NULL UNIQUE,
            scopes text[] NOT NULL CHECK (cardinality(scopes) > 0),
            rate_limit_per_minute integer NOT NULL DEFAULT 120 CHECK (rate_limit_per_minute BETWEEN 1 AND 6000),
            created_by uuid,
            expires_at timestamptz,
            revoked_at timestamptz,
            revoked_by uuid,
            last_used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_api_keys_tenant_id UNIQUE (tenant_id, id),
            {member_fk("api_keys", "created_by")}
        )
        """
    )
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY api_keys_tenant ON api_keys FOR ALL TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    # Only a usable key is returned: not revoked, not expired, and its creator is still a member.
    op.execute(
        """
        CREATE FUNCTION api_key_lookup(p_hash bytea)
        RETURNS TABLE (key_id uuid, tenant_id uuid, acting_user_id uuid, scopes text[], rate_limit_per_minute integer)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        BEGIN
            UPDATE api_keys k SET last_used_at = now()
            WHERE k.key_hash = p_hash AND (k.last_used_at IS NULL OR k.last_used_at < now() - interval '1 minute');
            RETURN QUERY
                SELECT k.id, k.tenant_id, k.created_by, k.scopes, k.rate_limit_per_minute FROM api_keys k
                WHERE k.key_hash = p_hash AND k.revoked_at IS NULL AND k.created_by IS NOT NULL
                  AND (k.expires_at IS NULL OR k.expires_at > now());
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION api_key_lookup(bytea) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION api_key_lookup(bytea) TO crm_app")

    tenant_table(
        "idempotency_keys",
        f"""
        api_key_id uuid NOT NULL,
        key text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
        request_hash text NOT NULL,
        state text NOT NULL DEFAULT 'in_progress' CHECK (state IN ('in_progress', 'completed')),
        status_code integer,
        response_body text,
        completed_at timestamptz,
        {tenant_fk("idempotency_keys", "api_key_id", "api_keys")},
        CONSTRAINT uq_idempotency_keys UNIQUE (tenant_id, api_key_id, key)
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_idempotency_keys_age ON idempotency_keys (created_at)")

    tenant_table(
        "webhook_endpoints",
        f"""
        url text NOT NULL CHECK (length(url) BETWEEN 10 AND 2000),
        description text,
        event_types text[] NOT NULL DEFAULT '{{*}}',
        status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
        secret_ciphertext bytea NOT NULL,
        secret_nonce bytea NOT NULL,
        secret_key_version text NOT NULL,
        previous_secret_ciphertext bytea,
        previous_secret_nonce bytea,
        previous_secret_key_version text,
        previous_secret_expires_at timestamptz,
        consecutive_failures integer NOT NULL DEFAULT 0,
        last_success_at timestamptz,
        last_failure_at timestamptz,
        last_error text,
        created_by uuid,
        {member_fk("webhook_endpoints", "created_by")}
        """,
    )
    tenant_table(
        "webhook_deliveries",
        f"""
        endpoint_id uuid NOT NULL,
        event_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'dead')),
        attempts integer NOT NULL DEFAULT 0,
        next_attempt_at timestamptz,
        last_status_code integer,
        last_error text,
        delivered_at timestamptz,
        {tenant_fk("webhook_deliveries", "endpoint_id", "webhook_endpoints")},
        {tenant_fk("webhook_deliveries", "event_id", "outbox_events")},
        CONSTRAINT uq_webhook_deliveries_once UNIQUE (tenant_id, endpoint_id, event_id)
        """,
    )
    op.execute(
        "CREATE INDEX ix_webhook_deliveries_endpoint ON webhook_deliveries (tenant_id, endpoint_id, created_at DESC)"
    )
    op.execute("ALTER TABLE outbox_events ADD COLUMN api_version text NOT NULL DEFAULT '2026-10-01'")


def downgrade() -> None:
    op.execute("ALTER TABLE outbox_events DROP COLUMN IF EXISTS api_version")
    for table in ("webhook_deliveries", "webhook_endpoints", "idempotency_keys"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS api_key_lookup(bytea)")
    op.execute("DROP TABLE IF EXISTS api_keys CASCADE")
