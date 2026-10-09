"""Person-recorded reply outcomes, meetings and proposal stages; narrower database access for billing events and audit purge.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- things only a person records, so reports can count them without guessing ---
    op.execute(
        """
        ALTER TABLE email_threads
            ADD COLUMN reply_outcome text CHECK (reply_outcome IN ('positive', 'neutral', 'negative')),
            ADD COLUMN reply_outcome_by uuid,
            ADD COLUMN reply_outcome_at timestamptz
        """
    )
    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_kind_check")
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN ('todo', 'call', 'follow_up', 'email', 'meeting'))"
    )
    op.execute("ALTER TABLE pipeline_stages ADD COLUMN is_proposal boolean NOT NULL DEFAULT false")
    op.execute("UPDATE pipeline_stages SET is_proposal = true WHERE name ILIKE 'proposal%'")
    op.execute(
        """
        CREATE FUNCTION pipeline_stage_flag_proposal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.is_proposal := NEW.is_proposal OR NEW.name ILIKE 'proposal%';
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER pipeline_stages_flag_proposal BEFORE INSERT ON pipeline_stages FOR EACH ROW EXECUTE FUNCTION pipeline_stage_flag_proposal()"
    )

    # --- billing events hold every tenant's provider references: no direct access for the application ---
    op.execute("REVOKE ALL ON billing_events FROM crm_app")
    op.execute(
        """
        CREATE FUNCTION billing_event_begin(p_id text, p_type text, p_customer text)
        RETURNS TABLE (already_done boolean, tenant_id uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        BEGIN
            INSERT INTO billing_events (provider_event_id, event_type, provider_customer_id) VALUES (p_id, p_type, p_customer)
            ON CONFLICT (provider_event_id) DO NOTHING;
            RETURN QUERY
                SELECT e.processed_at IS NOT NULL, (SELECT c.tenant_id FROM billing_customers c WHERE c.provider_customer_id = p_customer)
                FROM billing_events e WHERE e.provider_event_id = p_id;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION billing_event_finish(p_id text, p_outcome text) RETURNS void
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ UPDATE billing_events SET processed_at = now(), outcome = left(p_outcome, 200) WHERE provider_event_id = p_id $$
        """
    )
    for signature in ("billing_event_begin(text, text, text)", "billing_event_finish(text, text)"):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO crm_app")

    # --- the security log is append-only for the application; retention removes old entries of the current tenant only ---
    op.execute(
        """
        CREATE FUNCTION audit_events_purge(p_days integer) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_count integer;
        BEGIN
            IF app_current_tenant() IS NULL OR p_days < 365 THEN
                RAISE EXCEPTION 'a tenant and at least 365 days are required' USING ERRCODE = 'insufficient_privilege';
            END IF;
            DELETE FROM audit_events WHERE tenant_id = app_current_tenant() AND occurred_at < now() - make_interval(days => p_days);
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END
        $$
        """
    )
    # The log forces row security on its owner too, so the purge function needs its own narrow policies.
    op.execute(
        "CREATE POLICY audit_events_definer_select ON audit_events FOR SELECT TO crm_migrator USING (tenant_id = app_current_tenant())"
    )
    op.execute(
        "CREATE POLICY audit_events_definer_delete ON audit_events FOR DELETE TO crm_migrator USING (tenant_id = app_current_tenant())"
    )
    op.execute("REVOKE ALL ON FUNCTION audit_events_purge(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION audit_events_purge(integer) TO crm_app")

    # --- support access is granted to an email address; whether that address has an account is not revealed ---
    op.execute("DROP FUNCTION IF EXISTS user_id_by_email(text)")
    op.execute("ALTER TABLE support_grants ALTER COLUMN grantee_user_id DROP NOT NULL")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION support_access_begin(p_tenant uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_user uuid := app_current_user();
            v_email citext;
            v_grant uuid;
        BEGIN
            SELECT email INTO v_email FROM users WHERE id = v_user;
            IF v_email IS NULL OR EXISTS (SELECT 1 FROM memberships WHERE tenant_id = p_tenant AND user_id = v_user) THEN
                RETURN false;
            END IF;
            SELECT id INTO v_grant FROM support_grants
            WHERE tenant_id = p_tenant AND grantee_email = v_email AND revoked_at IS NULL AND expires_at > now()
            ORDER BY expires_at DESC LIMIT 1;
            IF v_grant IS NULL THEN
                RETURN false;
            END IF;
            UPDATE support_grants SET grantee_user_id = v_user, first_used_at = COALESCE(first_used_at, now())
            WHERE tenant_id = p_tenant AND grantee_email = v_email AND revoked_at IS NULL AND expires_at > now();
            INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_type, target_id, origin)
            VALUES (p_tenant, 'support', v_user, 'support.access_started', 'support_grant', v_grant::text, 'api');
            RETURN true;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM support_grants WHERE grantee_user_id IS NULL")
    op.execute("ALTER TABLE support_grants ALTER COLUMN grantee_user_id SET NOT NULL")
    op.execute("DROP POLICY IF EXISTS audit_events_definer_delete ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_definer_select ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_purge(integer)")
    op.execute("DROP FUNCTION IF EXISTS billing_event_finish(text, text)")
    op.execute("DROP FUNCTION IF EXISTS billing_event_begin(text, text, text)")
    op.execute("GRANT SELECT, INSERT, UPDATE ON billing_events TO crm_app")
    op.execute("DROP TRIGGER IF EXISTS pipeline_stages_flag_proposal ON pipeline_stages")
    op.execute("DROP FUNCTION IF EXISTS pipeline_stage_flag_proposal()")
    op.execute("ALTER TABLE pipeline_stages DROP COLUMN is_proposal")
    op.execute("UPDATE tasks SET kind = 'todo' WHERE kind = 'meeting'")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_kind_check")
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN ('todo', 'call', 'follow_up', 'email'))"
    )
    op.execute(
        "ALTER TABLE email_threads DROP COLUMN reply_outcome, DROP COLUMN reply_outcome_by, DROP COLUMN reply_outcome_at"
    )
