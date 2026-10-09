"""Retention settings, erasure tombstones, workspace deletion and time-limited support access.

Revision ID: 0012
Revises: 0011
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table = _base.tenant_table


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tenants
            ADD COLUMN retention jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN deletion_requested_at timestamptz,
            ADD COLUMN deletion_due_at timestamptz,
            ADD COLUMN deletion_requested_by uuid REFERENCES users (id) ON DELETE SET NULL
        """
    )
    op.execute(
        "GRANT UPDATE (retention, deletion_requested_at, deletion_due_at, deletion_requested_by) ON tenants TO crm_app"
    )
    # What was erased on request, kept only as keyed hashes so an import, a research run or the
    # mailbox cannot quietly bring it back. The values themselves are not stored.
    tenant_table(
        "erasure_tombstones",
        """
        kind text NOT NULL CHECK (kind IN ('email', 'domain', 'external_id', 'listing_id', 'phone')),
        value_hash text NOT NULL,
        reason text NOT NULL,
        erased_by uuid,
        CONSTRAINT uq_erasure_tombstones UNIQUE (tenant_id, kind, value_hash)
        """,
        updated_at=False,
    )
    # A record that a workspace existed and was deleted. No customer data.
    op.execute(
        """
        CREATE TABLE tenant_deletions (
            deleted_tenant_id uuid PRIMARY KEY,
            requested_at timestamptz NOT NULL,
            deleted_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION tenant_delete_due(p_tenant uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_requested timestamptz;
        BEGIN
            IF app_current_tenant() IS DISTINCT FROM p_tenant THEN
                RAISE EXCEPTION 'not this tenant' USING ERRCODE = 'insufficient_privilege';
            END IF;
            SELECT deletion_requested_at INTO v_requested FROM tenants
            WHERE id = p_tenant AND deletion_due_at IS NOT NULL AND deletion_due_at <= now() FOR UPDATE;
            IF v_requested IS NULL THEN
                RETURN false;  -- not requested, cancelled, or not due yet
            END IF;
            INSERT INTO tenant_deletions (deleted_tenant_id, requested_at) VALUES (p_tenant, v_requested);
            DELETE FROM tenants WHERE id = p_tenant;
            RETURN true;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION tenant_delete_due(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION tenant_delete_due(uuid) TO crm_app")

    # Support access: granted by the workspace owner to one named person, for a limited time.
    # Read before a tenant is selected, through a definer function, so ENABLE rather than FORCE.
    op.execute(
        """
        CREATE TABLE support_grants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            grantee_user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            include_communications boolean NOT NULL DEFAULT false,
            reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 10 AND 500),
            granted_by uuid REFERENCES users (id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            first_used_at timestamptz,
            CONSTRAINT ck_support_grants_window CHECK (expires_at <= created_at + interval '72 hours')
        )
        """
    )
    op.execute("ALTER TABLE support_grants ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY support_grants_tenant ON support_grants FOR ALL TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE FUNCTION support_access_begin(p_tenant uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_user uuid := app_current_user();
            v_grant uuid;
        BEGIN
            SELECT id INTO v_grant FROM support_grants
            WHERE tenant_id = p_tenant AND grantee_user_id = v_user AND revoked_at IS NULL AND expires_at > now()
            ORDER BY expires_at DESC LIMIT 1;
            IF v_grant IS NULL THEN
                RETURN false;
            END IF;
            UPDATE support_grants SET first_used_at = COALESCE(first_used_at, now()) WHERE id = v_grant;
            INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_type, target_id, origin)
            VALUES (p_tenant, 'support', v_user, 'support.access_started', 'support_grant', v_grant::text, 'api');
            RETURN true;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION support_access_begin(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION support_access_begin(uuid) TO crm_app")

    op.execute(
        """
        CREATE FUNCTION user_id_by_email(p_email text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT u.id FROM users u WHERE u.email = p_email::citext AND app_current_tenant() IS NOT NULL $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION user_id_by_email(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION user_id_by_email(text) TO crm_app")
    # Every workspace gets a daily retention job, in the database like every other schedule.
    op.execute(
        """
        CREATE FUNCTION tenant_schedule_retention() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        BEGIN
            INSERT INTO due_jobs (tenant_id, kind, unique_key, due_at)
            VALUES (NEW.id, 'retention.purge', 'daily', now() + interval '1 day')
            ON CONFLICT (tenant_id, kind, unique_key) DO NOTHING;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER tenants_schedule_retention AFTER INSERT ON tenants FOR EACH ROW EXECUTE FUNCTION tenant_schedule_retention()"
    )
    op.execute(
        "INSERT INTO due_jobs (tenant_id, kind, unique_key, due_at) SELECT id, 'retention.purge', 'daily', now() + interval '1 day' "
        "FROM tenants ON CONFLICT (tenant_id, kind, unique_key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS user_id_by_email(text)")
    op.execute("DROP TRIGGER IF EXISTS tenants_schedule_retention ON tenants")
    op.execute("DROP FUNCTION IF EXISTS tenant_schedule_retention()")
    op.execute("DELETE FROM due_jobs WHERE kind IN ('retention.purge', 'tenant.delete')")
    op.execute("DROP FUNCTION IF EXISTS support_access_begin(uuid)")
    op.execute("DROP TABLE IF EXISTS support_grants")
    op.execute("DROP FUNCTION IF EXISTS tenant_delete_due(uuid)")
    op.execute("DROP TABLE IF EXISTS tenant_deletions")
    op.execute("DROP TABLE IF EXISTS erasure_tombstones CASCADE")
    op.execute(
        "ALTER TABLE tenants DROP COLUMN retention, DROP COLUMN deletion_requested_at, DROP COLUMN deletion_due_at, "
        "DROP COLUMN deletion_requested_by"
    )
