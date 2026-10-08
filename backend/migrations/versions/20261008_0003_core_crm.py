"""Core CRM: companies, contacts, channels, leads, deals, pipelines, tasks, notes, tags,
custom fields, activities, attachments and saved views.

Every table here is tenant-owned: ``tenant_id`` is NOT NULL, row-level security is
FORCED, and relationships use composite ``(tenant_id, id)`` foreign keys so a row can
never point at another tenant's row.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "companies",
    "contacts",
    "contact_channels",
    "contact_restrictions",
    "pipelines",
    "pipeline_stages",
    "leads",
    "deals",
    "deal_stage_changes",
    "tasks",
    "notes",
    "tags",
    "taggings",
    "custom_field_definitions",
    "activities",
    "attachments",
    "saved_views",
)


def tenant_table(name: str, columns: str, *, updated_at: bool = True) -> None:
    """Create a tenant-owned table with forced row-level security."""
    op.execute(
        f"""
        CREATE TABLE {name} (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            {"updated_at timestamptz NOT NULL DEFAULT now()," if updated_at else ""}
            {columns},
            PRIMARY KEY (id),
            CONSTRAINT uq_{name}_tenant_id UNIQUE (tenant_id, id)
        )
        """
    )
    secure_tenant_table(name)
    if updated_at:
        op.execute(
            f"CREATE TRIGGER {name}_set_updated_at BEFORE UPDATE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def secure_tenant_table(name: str) -> None:
    # The policy also binds the table owner, so definer functions need a tenant context too.
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {name}_tenant ON {name} FOR ALL TO crm_app, crm_migrator
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )


def member_fk(table: str, column: str) -> str:
    return (
        f"CONSTRAINT fk_{table}_{column} FOREIGN KEY (tenant_id, {column}) "
        f"REFERENCES memberships (tenant_id, user_id) ON DELETE SET NULL ({column})"
    )


def tenant_fk(table: str, column: str, target: str, on_delete: str = "CASCADE") -> str:
    action = f"SET NULL ({column})" if on_delete == "SET NULL" else on_delete
    return (
        f"CONSTRAINT fk_{table}_{column} FOREIGN KEY (tenant_id, {column}) "
        f"REFERENCES {target} (tenant_id, id) ON DELETE {action}"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$
        """
    )

    tenant_table(
        "companies",
        f"""
        name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 300),
        external_id text,
        business_type text,
        industry_group text,
        city text,
        country text,
        language text,
        website_url text,
        domain text,
        owner_user_id uuid,
        source text NOT NULL DEFAULT 'manual',
        next_action text,
        next_action_at timestamptz,
        merged_into_id uuid,
        archived_at timestamptz,
        custom jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        {member_fk("companies", "owner_user_id")},
        {tenant_fk("companies", "merged_into_id", "companies", "SET NULL")},
        CONSTRAINT uq_companies_external_id UNIQUE (tenant_id, external_id)
        """,
    )
    op.execute("CREATE INDEX ix_companies_name_trgm ON companies USING gin (lower(name) gin_trgm_ops)")
    op.execute("CREATE INDEX ix_companies_tenant_domain ON companies (tenant_id, domain)")
    op.execute("CREATE INDEX ix_companies_tenant_created ON companies (tenant_id, created_at DESC, id)")

    tenant_table(
        "contacts",
        f"""
        company_id uuid NOT NULL,
        full_name text NOT NULL CHECK (length(btrim(full_name)) BETWEEN 1 AND 200),
        job_title text,
        language text,
        archived_at timestamptz,
        custom jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        {tenant_fk("contacts", "company_id", "companies")}
        """,
    )
    op.execute("CREATE INDEX ix_contacts_company ON contacts (tenant_id, company_id)")

    tenant_table(
        "contact_channels",
        f"""
        company_id uuid NOT NULL,
        contact_id uuid,
        kind text NOT NULL CHECK (kind IN ('phone', 'email', 'website', 'contact_page', 'social', 'messaging', 'other')),
        purpose text NOT NULL DEFAULT 'general'
            CHECK (purpose IN ('general', 'booking', 'delivery', 'emergency', 'unknown')),
        raw_value text NOT NULL CHECK (length(raw_value) BETWEEN 1 AND 500),
        normalized_value text,
        normalized_is_e164 boolean NOT NULL DEFAULT false,
        label text,
        position integer NOT NULL DEFAULT 0,
        source_type text NOT NULL DEFAULT 'manual_entry',
        source_url text,
        source_date date,
        verification_state text NOT NULL DEFAULT 'unverified'
            CHECK (verification_state IN ('unverified', 'verified', 'invalid')),
        do_not_contact boolean NOT NULL DEFAULT false,
        restriction_reason text,
        restricted_at timestamptz,
        restricted_by uuid,
        -- Emergency numbers are never offered for sales dialling unless explicitly allowed.
        allow_sales_use boolean GENERATED ALWAYS AS (purpose <> 'emergency' AND NOT do_not_contact) STORED,
        {tenant_fk("contact_channels", "company_id", "companies")},
        {tenant_fk("contact_channels", "contact_id", "contacts", "SET NULL")},
        CONSTRAINT ck_contact_channels_restriction CHECK (NOT do_not_contact OR restriction_reason IS NOT NULL)
        """,
    )
    op.execute("CREATE INDEX ix_contact_channels_company ON contact_channels (tenant_id, company_id, position)")
    op.execute(
        "CREATE INDEX ix_contact_channels_value ON contact_channels (tenant_id, kind, normalized_value) "
        "WHERE normalized_value IS NOT NULL"
    )

    # Restrictions apply to a whole company (optionally one person) for one kind of channel.
    # They are lifted, never deleted, so the reason for every block stays on record.
    tenant_table(
        "contact_restrictions",
        f"""
        company_id uuid NOT NULL,
        contact_id uuid,
        channel_kind text NOT NULL CHECK (channel_kind IN ('phone', 'email', 'any')),
        reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
        source text NOT NULL DEFAULT 'manual',
        created_by uuid,
        lifted_at timestamptz,
        lifted_by uuid,
        lift_reason text,
        {tenant_fk("contact_restrictions", "company_id", "companies")},
        {tenant_fk("contact_restrictions", "contact_id", "contacts", "SET NULL")},
        CONSTRAINT ck_contact_restrictions_lift CHECK ((lifted_at IS NULL) = (lift_reason IS NULL))
        """,
    )
    op.execute(
        "CREATE INDEX ix_contact_restrictions_active ON contact_restrictions "
        "(tenant_id, company_id, channel_kind) WHERE lifted_at IS NULL"
    )
    op.execute("REVOKE DELETE, TRUNCATE ON contact_restrictions FROM crm_app")

    tenant_table(
        "pipelines",
        """
        name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
        is_default boolean NOT NULL DEFAULT false,
        CONSTRAINT uq_pipelines_name UNIQUE (tenant_id, name)
        """,
    )
    op.execute("CREATE UNIQUE INDEX uq_pipelines_default ON pipelines (tenant_id) WHERE is_default")

    tenant_table(
        "pipeline_stages",
        f"""
        pipeline_id uuid NOT NULL,
        name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
        position integer NOT NULL,
        kind text NOT NULL DEFAULT 'open' CHECK (kind IN ('open', 'won', 'lost')),
        {tenant_fk("pipeline_stages", "pipeline_id", "pipelines")},
        CONSTRAINT uq_pipeline_stages_name UNIQUE (tenant_id, pipeline_id, name),
        CONSTRAINT uq_pipeline_stages_pipeline_id UNIQUE (tenant_id, pipeline_id, id)
        """,
    )

    tenant_table(
        "leads",
        f"""
        company_id uuid NOT NULL,
        contact_id uuid,
        external_id text,
        status text NOT NULL DEFAULT 'discovered'
            CHECK (status IN ('discovered', 'needs_review', 'qualified', 'disqualified', 'converted')),
        outreach_status text NOT NULL DEFAULT 'not_contacted',
        owner_user_id uuid,
        source text NOT NULL DEFAULT 'manual',
        disqualify_reason text,
        next_action text,
        next_action_at timestamptz,
        custom jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        {tenant_fk("leads", "company_id", "companies")},
        {tenant_fk("leads", "contact_id", "contacts", "SET NULL")},
        {member_fk("leads", "owner_user_id")},
        CONSTRAINT uq_leads_external_id UNIQUE (tenant_id, external_id),
        CONSTRAINT ck_leads_disqualified CHECK (status <> 'disqualified' OR disqualify_reason IS NOT NULL)
        """,
    )
    op.execute("CREATE INDEX ix_leads_tenant_status ON leads (tenant_id, status, created_at DESC)")
    op.execute("CREATE INDEX ix_leads_company ON leads (tenant_id, company_id)")

    tenant_table(
        "deals",
        f"""
        company_id uuid NOT NULL,
        lead_id uuid,
        contact_id uuid,
        pipeline_id uuid NOT NULL,
        stage_id uuid NOT NULL,
        title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 300),
        amount numeric(14, 2) CHECK (amount IS NULL OR amount >= 0),
        currency char(3) CHECK (currency IS NULL OR currency ~ '^[A-Z]{{3}}$'),
        original_amount numeric(14, 2),
        original_currency char(3),
        conversion_rate numeric(18, 8),
        conversion_source text,
        conversion_date date,
        expected_close_date date,
        owner_user_id uuid,
        next_action text,
        next_action_at timestamptz,
        loss_reason text,
        closed_at timestamptz,
        custom jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        {tenant_fk("deals", "company_id", "companies")},
        {tenant_fk("deals", "lead_id", "leads", "SET NULL")},
        {tenant_fk("deals", "contact_id", "contacts", "SET NULL")},
        {member_fk("deals", "owner_user_id")},
        CONSTRAINT fk_deals_stage FOREIGN KEY (tenant_id, pipeline_id, stage_id)
            REFERENCES pipeline_stages (tenant_id, pipeline_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_deals_amount_currency CHECK ((amount IS NULL) = (currency IS NULL)),
        CONSTRAINT ck_deals_conversion CHECK (
            (original_amount IS NULL AND original_currency IS NULL AND conversion_rate IS NULL)
            OR (original_amount IS NOT NULL AND original_currency IS NOT NULL
                AND conversion_rate IS NOT NULL AND conversion_source IS NOT NULL
                AND conversion_date IS NOT NULL)
        )
        """,
    )
    op.execute("CREATE INDEX ix_deals_stage ON deals (tenant_id, pipeline_id, stage_id)")
    op.execute("CREATE INDEX ix_deals_company ON deals (tenant_id, company_id)")

    tenant_table(
        "deal_stage_changes",
        f"""
        deal_id uuid NOT NULL,
        from_stage_id uuid,
        to_stage_id uuid NOT NULL,
        changed_by uuid,
        {tenant_fk("deal_stage_changes", "deal_id", "deals")}
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_deal_stage_changes_deal ON deal_stage_changes (tenant_id, deal_id, created_at)")

    entity_links = """
        company_id uuid,
        contact_id uuid,
        lead_id uuid,
        deal_id uuid,
        {company},
        {contact},
        {lead},
        {deal}
    """

    def links(table: str) -> str:
        return entity_links.format(
            company=tenant_fk(table, "company_id", "companies"),
            contact=tenant_fk(table, "contact_id", "contacts", "SET NULL"),
            lead=tenant_fk(table, "lead_id", "leads", "SET NULL"),
            deal=tenant_fk(table, "deal_id", "deals", "SET NULL"),
        )

    tenant_table(
        "tasks",
        f"""
        title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 300),
        description text,
        kind text NOT NULL DEFAULT 'todo' CHECK (kind IN ('todo', 'call', 'follow_up', 'email')),
        status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
        due_at timestamptz,
        assignee_user_id uuid,
        created_by uuid,
        completed_at timestamptz,
        {member_fk("tasks", "assignee_user_id")},
        {links("tasks")}
        """,
    )
    op.execute("CREATE INDEX ix_tasks_due ON tasks (tenant_id, status, due_at)")
    op.execute("CREATE INDEX ix_tasks_company ON tasks (tenant_id, company_id)")

    tenant_table(
        "notes",
        f"""
        body text NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 20000),
        author_user_id uuid,
        {links("notes")}
        """,
    )
    op.execute("CREATE INDEX ix_notes_company ON notes (tenant_id, company_id, created_at DESC)")

    tenant_table(
        "tags",
        """
        name citext NOT NULL CHECK (length(btrim(name::text)) BETWEEN 1 AND 120),
        CONSTRAINT uq_tags_name UNIQUE (tenant_id, name)
        """,
        updated_at=False,
    )
    op.execute(
        f"""
        CREATE TABLE taggings (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            tag_id uuid NOT NULL,
            entity_type text NOT NULL CHECK (entity_type IN ('company', 'contact', 'lead', 'deal')),
            entity_id uuid NOT NULL,
            origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import', 'automated')),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, tag_id, entity_type, entity_id),
            {tenant_fk("taggings", "tag_id", "tags")}
        )
        """
    )
    secure_tenant_table("taggings")
    op.execute("CREATE INDEX ix_taggings_entity ON taggings (tenant_id, entity_type, entity_id)")

    tenant_table(
        "custom_field_definitions",
        """
        entity_type text NOT NULL CHECK (entity_type IN ('company', 'contact', 'lead', 'deal')),
        key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,39}$'),
        label text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),
        field_type text NOT NULL CHECK (field_type IN ('text', 'number', 'date', 'boolean', 'select')),
        options jsonb NOT NULL DEFAULT '[]'::jsonb,
        CONSTRAINT uq_custom_field_key UNIQUE (tenant_id, entity_type, key)
        """,
    )

    tenant_table(
        "activities",
        f"""
        occurred_at timestamptz NOT NULL DEFAULT now(),
        kind text NOT NULL,
        summary text NOT NULL,
        actor_type text NOT NULL DEFAULT 'user' CHECK (actor_type IN ('user', 'integration', 'system')),
        actor_user_id uuid,
        origin text NOT NULL DEFAULT 'api',
        correlation_id text,
        data jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        {links("activities")}
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_activities_company ON activities (tenant_id, company_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_activities_deal ON activities (tenant_id, deal_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_activities_lead ON activities (tenant_id, lead_id, occurred_at DESC)")

    tenant_table(
        "attachments",
        f"""
        filename text NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
        content_type text NOT NULL,
        size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
        sha256 bytea NOT NULL,
        storage_key text NOT NULL,
        uploaded_by uuid,
        deleted_at timestamptz,
        {links("attachments")},
        CONSTRAINT uq_attachments_storage_key UNIQUE (storage_key)
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_attachments_company ON attachments (tenant_id, company_id)")

    tenant_table(
        "saved_views",
        """
        entity_type text NOT NULL CHECK (entity_type IN ('company', 'lead', 'deal', 'task', 'prospect')),
        name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
        owner_user_id uuid,
        is_shared boolean NOT NULL DEFAULT false,
        filters jsonb NOT NULL DEFAULT '{}'::jsonb,
        sort text
        """,
    )

    # A lead can point at the deal it became.
    op.execute("ALTER TABLE leads ADD COLUMN converted_deal_id uuid")
    op.execute("ALTER TABLE leads ADD " + tenant_fk("leads", "converted_deal_id", "deals", "SET NULL"))

    # Activities are a history: the application may add to it but not rewrite it.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON activities, deal_stage_changes FROM crm_app")
    # Merging reassigns history to the surviving company; only these columns may change.
    op.execute("GRANT UPDATE (company_id, contact_id, lead_id, deal_id) ON activities TO crm_app")

    # New tenants start with a default pipeline.
    op.execute(
        """
        CREATE FUNCTION tenant_seed_defaults() RETURNS trigger
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
            PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
            RETURN NEW;
        END $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION tenant_seed_defaults() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER tenants_seed_defaults AFTER INSERT ON tenants "
        "FOR EACH ROW EXECUTE FUNCTION tenant_seed_defaults()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tenants_seed_defaults ON tenants")
    op.execute("DROP FUNCTION IF EXISTS tenant_seed_defaults()")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
