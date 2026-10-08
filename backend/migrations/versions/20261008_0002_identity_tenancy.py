"""Identity, tenants, memberships, invitations, sessions and audit events.

Isolation model
---------------
* ``crm_app`` (runtime) owns nothing and cannot bypass row-level security.
* Policies read two transaction-local settings: ``app.user_id`` and ``app.tenant_id``.
  When they are unset every policy comparison is NULL, so nothing is visible.
* Operations that must run before a tenant is chosen (session lookup, login,
  creating a tenant, accepting an invitation) are narrow SECURITY DEFINER
  functions owned by the migrator. Identity tables therefore use ENABLE (not
  FORCE) row-level security. Tenant-owned business tables use FORCE.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = "('owner', 'administrator', 'sales_manager', 'representative', 'read_only')"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app_current_user() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
        """
    )

    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            oidc_issuer text NOT NULL,
            oidc_subject text NOT NULL,
            email citext NOT NULL,
            email_verified boolean NOT NULL DEFAULT false,
            display_name text NOT NULL DEFAULT '',
            is_platform_operator boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz,
            CONSTRAINT uq_users_identity UNIQUE (oidc_issuer, oidc_subject)
        )
        """
    )
    op.execute("CREATE INDEX ix_users_email ON users (email)")

    op.execute(
        """
        CREATE TABLE tenants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
            currency char(3) NOT NULL DEFAULT 'EUR' CHECK (currency ~ '^[A-Z]{3}$'),
            timezone text NOT NULL DEFAULT 'Europe/Sofia',
            created_by uuid REFERENCES users (id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE memberships (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            role text NOT NULL CHECK (role IN {ROLES}),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_memberships_user ON memberships (user_id)")

    op.execute(
        f"""
        CREATE TABLE invitations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            email citext NOT NULL,
            role text NOT NULL CHECK (role IN {ROLES}),
            token_hash bytea NOT NULL,
            invited_by uuid,
            expires_at timestamptz NOT NULL,
            accepted_at timestamptz,
            accepted_by uuid REFERENCES users (id) ON DELETE SET NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_invitations_token UNIQUE (token_hash),
            CONSTRAINT uq_invitations_tenant_id UNIQUE (tenant_id, id),
            -- Tenant-aware reference: the inviter must be a member of the same tenant.
            CONSTRAINT fk_invitations_inviter FOREIGN KEY (tenant_id, invited_by)
                REFERENCES memberships (tenant_id, user_id) ON DELETE SET NULL (invited_by)
        )
        """
    )
    op.execute("CREATE INDEX ix_invitations_tenant ON invitations (tenant_id, created_at DESC)")

    op.execute(
        """
        CREATE TABLE sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            token_hash bytea NOT NULL,
            csrf_token text NOT NULL,
            active_tenant_id uuid REFERENCES tenants (id) ON DELETE SET NULL,
            mfa_claimed boolean NOT NULL DEFAULT false,
            user_agent text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            CONSTRAINT uq_sessions_token UNIQUE (token_hash)
        )
        """
    )
    op.execute("CREATE INDEX ix_sessions_user ON sessions (user_id)")

    op.execute(
        """
        CREATE TABLE audit_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            actor_type text NOT NULL CHECK (actor_type IN ('user', 'integration', 'system', 'support')),
            actor_id uuid,
            action text NOT NULL,
            target_type text NOT NULL,
            target_id text,
            origin text NOT NULL DEFAULT 'api',
            correlation_id text,
            data jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_events_tenant_time ON audit_events (tenant_id, occurred_at DESC)")

    # --- privileges: narrow what the runtime role may do beyond the defaults -----------------
    op.execute("REVOKE INSERT, UPDATE, DELETE ON users FROM crm_app")
    op.execute("GRANT UPDATE (display_name) ON users TO crm_app")
    op.execute("REVOKE INSERT, DELETE ON tenants FROM crm_app")
    op.execute("REVOKE INSERT ON sessions FROM crm_app")
    op.execute("REVOKE UPDATE ON sessions FROM crm_app")
    op.execute("GRANT UPDATE (active_tenant_id, revoked_at, last_seen_at) ON sessions TO crm_app")
    # Audit events are append-only for the application.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM crm_app")

    # --- row-level security ----------------------------------------------------------------
    for table in ("users", "tenants", "memberships", "invitations", "sessions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY users_select ON users FOR SELECT TO crm_app USING (
            id = app_current_user()
            OR EXISTS (
                SELECT 1 FROM memberships m
                WHERE m.user_id = users.id AND m.tenant_id = app_current_tenant()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY users_update_self ON users FOR UPDATE TO crm_app
        USING (id = app_current_user()) WITH CHECK (id = app_current_user())
        """
    )

    op.execute(
        """
        CREATE POLICY memberships_select ON memberships FOR SELECT TO crm_app
        USING (tenant_id = app_current_tenant() OR user_id = app_current_user())
        """
    )
    op.execute(
        """
        CREATE POLICY memberships_insert ON memberships FOR INSERT TO crm_app
        WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE POLICY memberships_update ON memberships FOR UPDATE TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE POLICY memberships_delete ON memberships FOR DELETE TO crm_app
        USING (tenant_id = app_current_tenant())
        """
    )

    op.execute(
        """
        CREATE POLICY tenants_select ON tenants FOR SELECT TO crm_app USING (
            id = app_current_tenant()
            OR EXISTS (
                SELECT 1 FROM memberships m
                WHERE m.tenant_id = tenants.id AND m.user_id = app_current_user()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY tenants_update ON tenants FOR UPDATE TO crm_app
        USING (id = app_current_tenant()) WITH CHECK (id = app_current_tenant())
        """
    )

    op.execute(
        """
        CREATE POLICY invitations_tenant ON invitations FOR ALL TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )

    op.execute(
        """
        CREATE POLICY sessions_select ON sessions FOR SELECT TO crm_app
        USING (user_id = app_current_user())
        """
    )
    op.execute(
        """
        CREATE POLICY sessions_update ON sessions FOR UPDATE TO crm_app
        USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())
        """
    )
    op.execute(
        """
        CREATE POLICY sessions_delete ON sessions FOR DELETE TO crm_app
        USING (user_id = app_current_user())
        """
    )

    op.execute(
        """
        CREATE POLICY audit_events_select ON audit_events FOR SELECT TO crm_app
        USING (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE POLICY audit_events_insert ON audit_events FOR INSERT TO crm_app
        WITH CHECK (tenant_id = app_current_tenant())
        """
    )

    # FORCE applies to the table owner too. The definer functions below run as the owner
    # and must be able to append (never read or change) audit events.
    op.execute(
        """
        CREATE POLICY audit_events_definer_insert ON audit_events FOR INSERT TO crm_migrator
        WITH CHECK (true)
        """
    )

    # --- last-owner protection ---------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION memberships_keep_owner() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF OLD.role = 'owner' AND (TG_OP = 'DELETE' OR NEW.role <> 'owner') THEN
                -- Serialise concurrent owner changes within a tenant.
                PERFORM 1 FROM tenants WHERE id = OLD.tenant_id FOR UPDATE;
                IF NOT FOUND THEN
                    RETURN COALESCE(NEW, OLD);  -- the tenant itself is being deleted
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM memberships
                    WHERE tenant_id = OLD.tenant_id AND role = 'owner' AND user_id <> OLD.user_id
                ) THEN
                    RAISE EXCEPTION 'a tenant must keep at least one owner'
                        USING ERRCODE = 'check_violation', CONSTRAINT = 'memberships_keep_owner';
                END IF;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.tenant_id <> OLD.tenant_id OR NEW.user_id <> OLD.user_id THEN
                    RAISE EXCEPTION 'membership identity is immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                NEW.updated_at := now();
                RETURN NEW;
            END IF;
            RETURN OLD;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER memberships_keep_owner BEFORE UPDATE OR DELETE ON memberships
        FOR EACH ROW EXECUTE FUNCTION memberships_keep_owner()
        """
    )

    # --- pre-tenant operations ---------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION auth_upsert_user(
            p_issuer text, p_subject text, p_email text, p_email_verified boolean, p_name text
        ) RETURNS uuid
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            INSERT INTO users (oidc_issuer, oidc_subject, email, email_verified, display_name, last_login_at)
            VALUES (p_issuer, p_subject, p_email, p_email_verified, COALESCE(p_name, ''), now())
            ON CONFLICT (oidc_issuer, oidc_subject) DO UPDATE
                SET email = EXCLUDED.email,
                    email_verified = EXCLUDED.email_verified,
                    display_name = CASE WHEN users.display_name = '' THEN EXCLUDED.display_name
                                        ELSE users.display_name END,
                    last_login_at = now()
            RETURNING id
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION auth_create_session(
            p_user_id uuid, p_token_hash bytea, p_csrf text, p_expires_at timestamptz,
            p_user_agent text, p_mfa boolean
        ) RETURNS uuid
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            INSERT INTO sessions (user_id, token_hash, csrf_token, expires_at, user_agent,
                                  mfa_claimed, active_tenant_id)
            VALUES (
                p_user_id, p_token_hash, p_csrf, p_expires_at, left(COALESCE(p_user_agent, ''), 300),
                COALESCE(p_mfa, false),
                (SELECT tenant_id FROM memberships WHERE user_id = p_user_id
                 ORDER BY created_at LIMIT 1)
            )
            RETURNING id
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION auth_lookup_session(p_token_hash bytea)
        RETURNS TABLE (session_id uuid, user_id uuid, active_tenant_id uuid, csrf_token text,
                       expires_at timestamptz, mfa_claimed boolean)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            UPDATE sessions s SET last_seen_at = now()
            WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
            RETURNING s.id, s.user_id, s.active_tenant_id, s.csrf_token, s.expires_at, s.mfa_claimed
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION tenant_create(p_name text) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_user uuid := app_current_user();
            v_tenant uuid;
        BEGIN
            IF v_user IS NULL OR NOT EXISTS (SELECT 1 FROM users WHERE id = v_user) THEN
                RAISE EXCEPTION 'an authenticated user is required'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            INSERT INTO tenants (name, created_by) VALUES (btrim(p_name), v_user) RETURNING id INTO v_tenant;
            INSERT INTO memberships (tenant_id, user_id, role) VALUES (v_tenant, v_user, 'owner');
            INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_type, target_id)
            VALUES (v_tenant, 'user', v_user, 'tenant.created', 'tenant', v_tenant::text);
            RETURN v_tenant;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION invitation_peek(p_token_hash bytea)
        RETURNS TABLE (tenant_name text, email citext, role text, status text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            SELECT t.name, i.email, i.role,
                   CASE WHEN i.revoked_at IS NOT NULL THEN 'revoked'
                        WHEN i.accepted_at IS NOT NULL THEN 'accepted'
                        WHEN i.expires_at <= now() THEN 'expired'
                        ELSE 'pending' END
            FROM invitations i JOIN tenants t ON t.id = i.tenant_id
            WHERE i.token_hash = p_token_hash
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION invitation_accept(p_token_hash bytea) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_user users%ROWTYPE;
            v_inv invitations%ROWTYPE;
        BEGIN
            SELECT * INTO v_user FROM users WHERE id = app_current_user();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'an authenticated user is required'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            SELECT * INTO v_inv FROM invitations WHERE token_hash = p_token_hash FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'invitation not found' USING ERRCODE = 'no_data_found';
            END IF;
            IF v_inv.revoked_at IS NOT NULL OR v_inv.accepted_at IS NOT NULL
               OR v_inv.expires_at <= now() THEN
                RAISE EXCEPTION 'invitation is no longer valid' USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
            IF v_inv.email <> v_user.email OR NOT v_user.email_verified THEN
                RAISE EXCEPTION 'invitation was issued to a different verified email'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            INSERT INTO memberships (tenant_id, user_id, role)
            VALUES (v_inv.tenant_id, v_user.id, v_inv.role)
            ON CONFLICT (tenant_id, user_id) DO NOTHING;
            UPDATE invitations SET accepted_at = now(), accepted_by = v_user.id WHERE id = v_inv.id;
            INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_type, target_id, data)
            VALUES (v_inv.tenant_id, 'user', v_user.id, 'invitation.accepted', 'invitation',
                    v_inv.id::text, jsonb_build_object('role', v_inv.role));
            RETURN v_inv.tenant_id;
        END
        $$
        """
    )

    for signature in (
        "app_current_user()",
        "auth_upsert_user(text, text, text, boolean, text)",
        "auth_create_session(uuid, bytea, text, timestamptz, text, boolean)",
        "auth_lookup_session(bytea)",
        "tenant_create(text)",
        "invitation_peek(bytea)",
        "invitation_accept(bytea)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO crm_app")
    op.execute("REVOKE ALL ON FUNCTION memberships_keep_owner() FROM PUBLIC")


def downgrade() -> None:
    for table in ("audit_events", "sessions", "invitations", "memberships", "tenants", "users"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for signature in (
        "invitation_accept(bytea)",
        "invitation_peek(bytea)",
        "tenant_create(text)",
        "auth_lookup_session(bytea)",
        "auth_create_session(uuid, bytea, text, timestamptz, text, boolean)",
        "auth_upsert_user(text, text, text, boolean, text)",
        "memberships_keep_owner()",
        "app_current_user()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
