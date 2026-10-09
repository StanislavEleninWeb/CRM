"""Research configurations, runs, candidates and field-level source policies.

Revision ID: 0007
Revises: 0006
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table, tenant_fk, member_fk = _base.tenant_table, _base.tenant_fk, _base.member_fk
_research = importlib.import_module("migrations.versions.20261008_0004_research_scoring_import")

TABLES = ("source_policies", "research_configs", "research_runs", "research_queries", "research_candidates", "due_jobs")

# (source_type, field, may_store, retention_days, may_export, may_score, status, note)
DEFAULT_POLICIES = (
    ("user_import", "*", True, None, True, True, "approved", "Uploaded by a workspace member."),
    ("manual_entry", "*", True, None, True, True, "approved", "Typed by a workspace member."),
    ("official_website", "*", True, None, True, True, "approved", "Read from the business's own public website."),
    ("ai_derived", "*", True, None, True, True, "approved", "Follows the most restrictive source it was derived from."),
    (
        "google_places",
        "place_id",
        True,
        None,
        True,
        False,
        "approved",
        "Place IDs may be stored under the provider's documented exemption.",
    ),
    (
        "google_places",
        "*",
        False,
        None,
        False,
        False,
        "unverified",
        "Terms for an EEA-billed account are not verified. Nothing except the place ID is stored.",
    ),
    ("scraping_provider", "*", False, None, False, False, "unverified", "Source terms have not been reviewed."),
)


def _seed_values(tenant_expr: str) -> str:
    rows = ", ".join(
        f"({tenant_expr}, '{s}', '{f}', {str(store).lower()}, {days if days is not None else 'NULL'}, "
        f"{str(export).lower()}, {str(score).lower()}, '{status}', '{note.replace(chr(39), chr(39) * 2)}')"
        for s, f, store, days, export, score, status, note in DEFAULT_POLICIES
    )
    return (
        "INSERT INTO source_policies (tenant_id, source_type, field, may_store, retention_days, may_export, "
        f"may_use_for_scoring, status, note) VALUES {rows} ON CONFLICT DO NOTHING"
    )


def upgrade() -> None:
    tenant_table(
        "source_policies",
        """
        source_type text NOT NULL,
        field text NOT NULL,
        may_store boolean NOT NULL,
        retention_days integer CHECK (retention_days IS NULL OR retention_days > 0),
        may_export boolean NOT NULL,
        may_use_for_scoring boolean NOT NULL,
        attribution text,
        status text NOT NULL CHECK (status IN ('approved', 'unverified', 'quarantined')),
        note text,
        reviewed_by uuid,
        reviewed_at timestamptz,
        CONSTRAINT uq_source_policies UNIQUE (tenant_id, source_type, field),
        -- An unverified or quarantined source can never be stored or exported, whatever the flags say.
        CONSTRAINT ck_source_policies_unverified CHECK (status = 'approved' OR (NOT may_store AND NOT may_export AND NOT may_use_for_scoring))
        """,
    )

    tenant_table(
        "research_configs",
        f"""
        name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
        country text NOT NULL,
        cities text[] NOT NULL CHECK (cardinality(cities) BETWEEN 1 AND 50),
        categories text[] NOT NULL CHECK (cardinality(categories) BETWEEN 1 AND 50),
        services text[] NOT NULL DEFAULT '{{}}',
        ideal_customer text,
        exclusions jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        depth text NOT NULL DEFAULT 'homepage' CHECK (depth IN ('listing_only', 'homepage', 'site')),
        draft_language text NOT NULL DEFAULT 'bg',
        cost_cap numeric(14, 4) NOT NULL CHECK (cost_cap >= 0),
        candidate_cap integer NOT NULL CHECK (candidate_cap BETWEEN 1 AND 2000),
        qualified_target integer NOT NULL CHECK (qualified_target BETWEEN 1 AND 2000),
        cadence text NOT NULL DEFAULT 'manual' CHECK (cadence IN ('manual', 'daily', 'weekly')),
        run_at_local_time time NOT NULL DEFAULT '06:00',
        next_run_at timestamptz,
        discovery_connection_id uuid,
        model_connection_id uuid,
        is_active boolean NOT NULL DEFAULT true,
        created_by uuid,
        {tenant_fk("research_configs", "discovery_connection_id", "provider_connections", "SET NULL")},
        {tenant_fk("research_configs", "model_connection_id", "provider_connections", "SET NULL")},
        CONSTRAINT uq_research_configs_name UNIQUE (tenant_id, name)
        """,
    )
    # The schedule lives here as due rows; the scheduler polls it. Nothing is parked in the task queue.
    op.execute(
        "CREATE INDEX ix_research_configs_due ON research_configs (next_run_at) WHERE is_active AND next_run_at IS NOT NULL"
    )

    tenant_table(
        "research_runs",
        f"""
        config_id uuid NOT NULL,
        mode text NOT NULL CHECK (mode IN ('discover', 'refresh')),
        trigger text NOT NULL CHECK (trigger IN ('manual', 'schedule')),
        status text NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'paused', 'cancelling', 'cancelled', 'completed', 'failed')),
        settings jsonb NOT NULL,
        checkpoint jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        counters jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        summary jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        estimated_cost numeric(14, 4) NOT NULL DEFAULT 0,
        error text,
        requested_by uuid,
        started_at timestamptz,
        finished_at timestamptz,
        lease_token uuid,
        lease_expires_at timestamptz,
        {tenant_fk("research_runs", "config_id", "research_configs")}
        """,
    )
    op.execute(
        "CREATE INDEX ix_research_runs_work ON research_runs (status, lease_expires_at) WHERE status IN ('queued', 'running', 'cancelling')"
    )

    tenant_table(
        "research_queries",
        f"""
        run_id uuid NOT NULL,
        position integer NOT NULL,
        city text NOT NULL,
        category text NOT NULL,
        provider text NOT NULL,
        query_text text NOT NULL,
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
        result_count integer,
        coverage_gap text,
        executed_at timestamptz,
        {tenant_fk("research_queries", "run_id", "research_runs")},
        CONSTRAINT uq_research_queries_position UNIQUE (tenant_id, run_id, position)
        """,
        updated_at=False,
    )

    tenant_table(
        "research_candidates",
        f"""
        run_id uuid NOT NULL,
        query_id uuid,
        state text NOT NULL DEFAULT 'new'
            CHECK (state IN ('new', 'duplicate', 'excluded', 'needs_review', 'qualified', 'rejected', 'promoted', 'error')),
        state_reason text,
        listing_provider text,
        listing_id_type text NOT NULL DEFAULT 'unknown' CHECK (listing_id_type IN ('place_id', 'cid', 'unknown')),
        listing_id text,
        name text,
        name_source text,
        city text,
        category text,
        website_url text,
        website_source text,
        website_match text NOT NULL DEFAULT 'none' CHECK (website_match IN ('confirmed', 'ambiguous', 'none')),
        domain text,
        duplicate_lead_id uuid,
        inspection jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        proposal jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        validation_issues jsonb NOT NULL DEFAULT '[]'::jsonb,
        dropped_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
        model_name text,
        prompt_version text,
        promoted_lead_id uuid,
        reviewed_by uuid,
        reviewed_at timestamptz,
        expires_at timestamptz,
        {tenant_fk("research_candidates", "run_id", "research_runs")},
        {tenant_fk("research_candidates", "query_id", "research_queries", "SET NULL")},
        {tenant_fk("research_candidates", "duplicate_lead_id", "leads", "SET NULL")},
        {tenant_fk("research_candidates", "promoted_lead_id", "leads", "SET NULL")}
        """,
    )
    op.execute("CREATE INDEX ix_research_candidates_run ON research_candidates (tenant_id, run_id, state)")
    op.execute(
        "CREATE INDEX ix_research_candidates_listing ON research_candidates (tenant_id, listing_id_type, listing_id) WHERE listing_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_research_candidates_expiry ON research_candidates (expires_at) WHERE expires_at IS NOT NULL"
    )

    # --- due rows: the one place where "do this later" is recorded -----------------------------
    # Tenant code sees only its own rows. The scheduler claims rows for every tenant through
    # due_jobs_claim(), a definer function; that is why this table uses ENABLE, not FORCE.
    op.execute(
        """
        CREATE TABLE due_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            kind text NOT NULL,
            ref_id uuid,
            unique_key text NOT NULL,
            due_at timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'done', 'cancelled', 'failed')),
            attempts integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 20,
            lease_token uuid,
            lease_expires_at timestamptz,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT uq_due_jobs_key UNIQUE (tenant_id, kind, unique_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_due_jobs_due ON due_jobs (due_at) WHERE status IN ('pending', 'claimed')")
    op.execute("ALTER TABLE due_jobs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY due_jobs_tenant ON due_jobs FOR ALL TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE FUNCTION due_jobs_claim(p_limit integer, p_lease_seconds integer)
        RETURNS TABLE (id uuid, tenant_id uuid, kind text, ref_id uuid, lease_token uuid, attempts integer, payload jsonb)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            UPDATE due_jobs j
            SET status = 'claimed', lease_token = gen_random_uuid(),
                lease_expires_at = now() + make_interval(secs => LEAST(GREATEST(p_lease_seconds, 10), 3600)),
                attempts = j.attempts + 1
            WHERE j.id IN (
                SELECT d.id FROM due_jobs d
                WHERE d.due_at <= now()
                  AND (d.status = 'pending' OR (d.status = 'claimed' AND d.lease_expires_at < now()))
                ORDER BY d.due_at
                LIMIT LEAST(GREATEST(p_limit, 1), 500)
                FOR UPDATE SKIP LOCKED
            )
            RETURNING j.id, j.tenant_id, j.kind, j.ref_id, j.lease_token, j.attempts, j.payload
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION due_jobs_backlog() RETURNS TABLE (due integer, oldest_due_seconds integer)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            SELECT count(*)::integer, COALESCE(extract(epoch FROM now() - min(due_at)), 0)::integer
            FROM due_jobs WHERE due_at <= now() AND status IN ('pending', 'claimed')
        $$
        """
    )
    for signature in ("due_jobs_claim(integer, integer)", "due_jobs_backlog()"):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO crm_app")

    # Source policies for new tenants and for those that already exist.
    seed = _seed_values("NEW.id")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION tenant_seed_defaults() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_pipeline uuid;
            v_previous text := current_setting('app.tenant_id', true);
        BEGIN
            PERFORM set_config('app.tenant_id', NEW.id::text, true);
            INSERT INTO pipelines (tenant_id, name, is_default) VALUES (NEW.id, 'Sales', true)
            RETURNING id INTO v_pipeline;
            INSERT INTO pipeline_stages (tenant_id, pipeline_id, name, position, kind)
            SELECT NEW.id, v_pipeline, s.name, s.position, s.kind
            FROM (VALUES ('New', 10, 'open'), ('Contacted', 20, 'open'), ('Replied', 30, 'open'),
                         ('Meeting booked', 40, 'open'), ('Proposal sent', 50, 'open'),
                         ('Negotiation', 60, 'open'), ('Won', 70, 'won'), ('Lost', 80, 'lost'))
                 AS s (name, position, kind);
            INSERT INTO rubric_versions (tenant_id, version, name, components, is_active)
            VALUES (NEW.id, 1, 'Default prospect rubric', {_research.DEFAULT_RUBRIC}, true);
            {seed};
            PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        f"""
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN SELECT id FROM tenants LOOP
                PERFORM set_config('app.tenant_id', t.id::text, true);
                {_seed_values("t.id")};
            END LOOP;
            PERFORM set_config('app.tenant_id', '', true);
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS due_jobs_claim(integer, integer)")
    op.execute("DROP FUNCTION IF EXISTS due_jobs_backlog()")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
