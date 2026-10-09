"""Research schema, deterministic scoring, imports and shortlist snapshots.

Observations (what was seen), hypotheses (what is assumed) and scores (how a lead is
prioritised) are stored separately. Confidence is independent of score. Everything
imported keeps its raw value and starts as user-imported and unverified.

Revision ID: 0004
Revises: 0003
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table, tenant_fk, member_fk = _base.tenant_table, _base.tenant_fk, _base.member_fk

TABLES = (
    "service_catalog",
    "rubric_versions",
    "imports",
    "import_rows",
    "lead_assessments",
    "observations",
    "hypotheses",
    "lead_scores",
    "shortlists",
    "shortlist_entries",
)

DEFAULT_RUBRIC = """'[
  {"key": "evidence", "label": "Evidence strength", "max": 30},
  {"key": "relevance", "label": "Relevance to our services", "max": 25},
  {"key": "value", "label": "Plausible business value", "max": 20},
  {"key": "reachability", "label": "Reachability", "max": 15},
  {"key": "activity", "label": "Signs of current business activity", "max": 10}
]'::jsonb"""

SOURCE_TYPES = "('user_import', 'manual_entry', 'official_website', 'google_places', 'scraping_provider', 'ai_derived')"
VERIFICATION = "('unverified', 'verified', 'contradicted', 'stale')"


def upgrade() -> None:
    tenant_table(
        "service_catalog",
        """
        name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
        description text,
        is_active boolean NOT NULL DEFAULT true,
        CONSTRAINT uq_service_catalog_name UNIQUE (tenant_id, name)
        """,
    )

    tenant_table(
        "rubric_versions",
        """
        version integer NOT NULL,
        name text NOT NULL,
        components jsonb NOT NULL,
        tier_a_min integer NOT NULL DEFAULT 80,
        tier_b_min integer NOT NULL DEFAULT 60,
        is_active boolean NOT NULL DEFAULT false,
        CONSTRAINT uq_rubric_versions_version UNIQUE (tenant_id, version),
        CONSTRAINT ck_rubric_tiers CHECK (tier_a_min > tier_b_min AND tier_b_min > 0)
        """,
        updated_at=False,
    )
    op.execute("CREATE UNIQUE INDEX uq_rubric_versions_active ON rubric_versions (tenant_id) WHERE is_active")
    # A rubric version is immutable once created: changing the rules means a new version.
    op.execute("REVOKE UPDATE (components, tier_a_min, tier_b_min, version), DELETE ON rubric_versions FROM crm_app")

    tenant_table(
        "imports",
        f"""
        kind text NOT NULL CHECK (kind IN ('xlsx', 'csv')),
        filename text NOT NULL,
        size_bytes bigint NOT NULL,
        sha256 bytea NOT NULL,
        storage_key text NOT NULL,
        status text NOT NULL DEFAULT 'uploaded'
            CHECK (status IN ('uploaded', 'parsing', 'ready', 'committing', 'committed', 'failed')),
        mapping jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        report jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        context jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        result jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        error text,
        created_by uuid,
        committed_at timestamptz,
        {member_fk("imports", "created_by")}
        """,
    )

    tenant_table(
        "import_rows",
        f"""
        import_id uuid NOT NULL,
        row_number integer NOT NULL,
        external_id text,
        data jsonb NOT NULL,
        raw jsonb NOT NULL,
        issues jsonb NOT NULL DEFAULT '[]'::jsonb,
        duplicate_lead_id uuid,
        duplicate_reason text,
        action text NOT NULL DEFAULT 'create' CHECK (action IN ('create', 'update', 'skip')),
        lead_id uuid,
        {tenant_fk("import_rows", "import_id", "imports")},
        CONSTRAINT uq_import_rows_number UNIQUE (tenant_id, import_id, row_number)
        """,
        updated_at=False,
    )

    tenant_table(
        "lead_assessments",
        f"""
        lead_id uuid NOT NULL,
        checked_on date,
        confidence text NOT NULL DEFAULT 'unknown' CHECK (confidence IN ('high', 'medium', 'low', 'unknown')),
        source_type text NOT NULL DEFAULT 'user_import' CHECK (source_type IN {SOURCE_TYPES}),
        verification_state text NOT NULL DEFAULT 'unverified' CHECK (verification_state IN {VERIFICATION}),
        import_id uuid,

        website_status_raw text,
        website_base text NOT NULL DEFAULT 'unknown'
            CHECK (website_base IN ('loads', 'unreachable', 'unrelated_content', 'not_found', 'unknown')),
        website_availability text NOT NULL DEFAULT 'unknown'
            CHECK (website_availability IN ('available', 'unavailable', 'none', 'unknown')),
        website_transport text NOT NULL DEFAULT 'unknown'
            CHECK (website_transport IN ('https', 'http_only', 'https_broken', 'unknown')),
        website_issue_flags text[] NOT NULL DEFAULT '{{}}',
        website_match text NOT NULL DEFAULT 'unknown' CHECK (website_match IN ('assumed', 'none', 'unknown')),

        listing_url text,
        listing_id_type text NOT NULL DEFAULT 'unknown' CHECK (listing_id_type IN ('place_id', 'cid', 'unknown')),
        listing_id text,

        contact_states jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        preferred_channel text CHECK (preferred_channel IN ('phone', 'email', 'contact_page', 'social', 'other')),
        fallback_channels text[] NOT NULL DEFAULT '{{}}',
        channel_instruction text,
        channel_recommendation_raw text,

        service_id uuid,
        recommended_service_raw text,
        recommended_services text[] NOT NULL DEFAULT '{{}}',
        fit_explanation text,
        proposed_benefit text,
        outreach_opening text,
        discovery_question text,
        outreach_status_raw text,
        notes text,
        raw_row jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        content_hash text NOT NULL DEFAULT '',
        is_current boolean NOT NULL DEFAULT true,
        superseded_at timestamptz,

        {tenant_fk("lead_assessments", "lead_id", "leads")},
        {tenant_fk("lead_assessments", "import_id", "imports", "SET NULL")},
        {tenant_fk("lead_assessments", "service_id", "service_catalog", "SET NULL")}
        """,
    )
    # One current assessment per lead; earlier ones are kept as the evidence history.
    op.execute(
        "CREATE UNIQUE INDEX uq_lead_assessments_current ON lead_assessments (tenant_id, lead_id) WHERE is_current"
    )
    op.execute("CREATE INDEX ix_lead_assessments_confidence ON lead_assessments (tenant_id, confidence)")
    op.execute("CREATE INDEX ix_lead_assessments_checked ON lead_assessments (tenant_id, checked_on)")

    finding = f"""
        lead_id uuid NOT NULL,
        text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 5000),
        source_type text NOT NULL DEFAULT 'user_import' CHECK (source_type IN {SOURCE_TYPES}),
        import_id uuid,
        created_by uuid,
    """
    tenant_table(
        "observations",
        finding
        + f"""
        evidence_url text,
        observed_on date,
        verification_state text NOT NULL DEFAULT 'unverified' CHECK (verification_state IN {VERIFICATION}),
        verified_by uuid,
        verified_at timestamptz,
        superseded_at timestamptz,
        {tenant_fk("observations", "lead_id", "leads")},
        {tenant_fk("observations", "import_id", "imports", "SET NULL")}
        """,
    )
    op.execute("CREATE INDEX ix_observations_lead ON observations (tenant_id, lead_id, created_at)")
    tenant_table(
        "hypotheses",
        finding
        + f"""
        status text NOT NULL DEFAULT 'unconfirmed' CHECK (status IN ('unconfirmed', 'confirmed', 'rejected')),
        resolved_by uuid,
        resolved_at timestamptz,
        superseded_at timestamptz,
        {tenant_fk("hypotheses", "lead_id", "leads")},
        {tenant_fk("hypotheses", "import_id", "imports", "SET NULL")}
        """,
    )
    op.execute("CREATE INDEX ix_hypotheses_lead ON hypotheses (tenant_id, lead_id, created_at)")

    tenant_table(
        "lead_scores",
        f"""
        lead_id uuid NOT NULL,
        rubric_version_id uuid NOT NULL,
        components jsonb NOT NULL,
        total integer NOT NULL CHECK (total >= 0),
        tier text NOT NULL CHECK (tier IN ('A', 'B', 'C')),
        origin text NOT NULL CHECK (origin IN ('import', 'manual', 'override', 'ai_proposal', 'recompute')),
        reasons jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        override_reason text,
        source_total integer,
        source_tier text,
        is_current boolean NOT NULL DEFAULT true,
        import_id uuid,
        created_by uuid,
        {tenant_fk("lead_scores", "lead_id", "leads")},
        {tenant_fk("lead_scores", "rubric_version_id", "rubric_versions", "RESTRICT")},
        {tenant_fk("lead_scores", "import_id", "imports", "SET NULL")},
        CONSTRAINT ck_lead_scores_override CHECK (origin <> 'override' OR override_reason IS NOT NULL)
        """,
        updated_at=False,
    )
    op.execute("CREATE UNIQUE INDEX uq_lead_scores_current ON lead_scores (tenant_id, lead_id) WHERE is_current")
    op.execute("CREATE INDEX ix_lead_scores_rank ON lead_scores (tenant_id, total DESC) WHERE is_current")
    # Score history is append-only; the only permitted change is retiring the current flag.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON lead_scores FROM crm_app")
    op.execute("GRANT UPDATE (is_current) ON lead_scores TO crm_app")

    tenant_table(
        "shortlists",
        f"""
        shortlist_date date NOT NULL,
        name text NOT NULL,
        origin text NOT NULL CHECK (origin IN ('import', 'generated')),
        kind text NOT NULL DEFAULT 'score_ranked' CHECK (kind IN ('score_ranked', 'call_queue')),
        requested_size integer NOT NULL CHECK (requested_size > 0),
        import_id uuid,
        created_by uuid,
        note text,
        {tenant_fk("shortlists", "import_id", "imports", "SET NULL")}
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_shortlists_date ON shortlists (tenant_id, shortlist_date DESC)")
    tenant_table(
        "shortlist_entries",
        f"""
        shortlist_id uuid NOT NULL,
        lead_id uuid NOT NULL,
        rank integer NOT NULL CHECK (rank > 0),
        score_at_snapshot integer,
        tier_at_snapshot text,
        is_filler boolean NOT NULL DEFAULT false,
        reason text,
        {tenant_fk("shortlist_entries", "shortlist_id", "shortlists")},
        {tenant_fk("shortlist_entries", "lead_id", "leads")},
        CONSTRAINT uq_shortlist_entries_rank UNIQUE (tenant_id, shortlist_id, rank),
        CONSTRAINT uq_shortlist_entries_lead UNIQUE (tenant_id, shortlist_id, lead_id)
        """,
        updated_at=False,
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON shortlist_entries FROM crm_app")

    # Default rubric for new tenants, and for tenants that already exist.
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
            VALUES (NEW.id, 1, 'Default prospect rubric', {DEFAULT_RUBRIC}, true);
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
                INSERT INTO rubric_versions (tenant_id, version, name, components, is_active)
                VALUES (t.id, 1, 'Default prospect rubric', {DEFAULT_RUBRIC}, true)
                ON CONFLICT DO NOTHING;
            END LOOP;
            PERFORM set_config('app.tenant_id', '', true);
        END $$
        """
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
