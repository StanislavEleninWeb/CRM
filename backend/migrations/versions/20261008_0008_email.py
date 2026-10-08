"""Email: eligibility policy, suppression, mailboxes, threads, messages, drafts and send intents.

Revision ID: 0008
Revises: 0007
"""

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_base = importlib.import_module("migrations.versions.20261008_0003_core_crm")
tenant_table, tenant_fk, member_fk = _base.tenant_table, _base.tenant_fk, _base.member_fk

TABLES = (
    "outreach_policies",
    "email_suppressions",
    "email_consents",
    "recipient_profiles",
    "regulatory_sources",
    "regulatory_entries",
    "mailboxes",
    "email_threads",
    "email_messages",
    "email_drafts",
    "send_intents",
    "eligibility_decisions",
)

# A draft policy for Bulgarian unsolicited commercial email. It blocks unsolicited sending until a
# named person approves it, after the current legal text and register process have been confirmed.
DRAFT_POLICY_RULES = """'{
  "basis": "Bulgarian Electronic Commerce Act, Article 6 (to be confirmed against the current text)",
  "requires_unsolicited_label": true,
  "label_text": "Непоискано търговско съобщение",
  "requires_sender_identity": true,
  "requires_register_check": true,
  "register_max_age_days": 7,
  "natural_person": "block_without_consent",
  "consumer_context": "block_without_consent",
  "sole_trader": "review",
  "unknown_recipient": "review"
}'::jsonb"""


def upgrade() -> None:
    tenant_table(
        "outreach_policies",
        """
        jurisdiction text NOT NULL,
        version text NOT NULL,
        status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'retired')),
        rules jsonb NOT NULL,
        source_checked_on date,
        source_note text,
        approved_by uuid,
        approved_at timestamptz,
        approval_note text,
        CONSTRAINT uq_outreach_policies_version UNIQUE (tenant_id, jurisdiction, version),
        CONSTRAINT ck_outreach_policies_approval CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND approval_note IS NOT NULL))
        """,
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_outreach_policies_approved ON outreach_policies (tenant_id, jurisdiction) WHERE status = 'approved'"
    )
    op.execute("ALTER TABLE tenants ADD COLUMN sender_identity text")

    tenant_table(
        "email_suppressions",
        """
        scope text NOT NULL CHECK (scope IN ('address', 'domain')),
        value citext NOT NULL,
        reason text NOT NULL CHECK (reason IN ('opt_out', 'permanent_bounce', 'complaint', 'manual', 'regulatory')),
        source text NOT NULL DEFAULT 'manual',
        note text,
        created_by uuid,
        lifted_at timestamptz,
        lifted_by uuid,
        lift_note text,
        CONSTRAINT ck_email_suppressions_lift CHECK ((lifted_at IS NULL) = (lift_note IS NULL))
        """,
        updated_at=False,
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_email_suppressions_active ON email_suppressions (tenant_id, scope, value) WHERE lifted_at IS NULL"
    )
    # A suppression can be lifted with a recorded reason, never deleted. Re-imports cannot remove one.
    op.execute("REVOKE DELETE, TRUNCATE ON email_suppressions FROM crm_app")
    op.execute("REVOKE UPDATE ON email_suppressions FROM crm_app")
    op.execute("GRANT UPDATE (lifted_at, lifted_by, lift_note) ON email_suppressions TO crm_app")

    tenant_table(
        "email_consents",
        """
        address citext NOT NULL,
        scope text NOT NULL CHECK (length(btrim(scope)) BETWEEN 3 AND 500),
        kind text NOT NULL CHECK (kind IN ('requested_follow_up', 'opt_in')),
        source text NOT NULL,
        evidence text,
        obtained_at timestamptz NOT NULL,
        recorded_by uuid,
        revoked_at timestamptz
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_email_consents_address ON email_consents (tenant_id, address) WHERE revoked_at IS NULL")

    tenant_table(
        "recipient_profiles",
        """
        address citext NOT NULL,
        legal_form text NOT NULL DEFAULT 'unknown' CHECK (legal_form IN ('legal_person', 'sole_trader', 'natural_person', 'unknown')),
        context text NOT NULL DEFAULT 'unknown' CHECK (context IN ('business', 'consumer', 'unknown')),
        jurisdiction text NOT NULL DEFAULT 'BG',
        evidence text,
        evidence_url text,
        classified_by uuid,
        classified_at timestamptz,
        CONSTRAINT uq_recipient_profiles_address UNIQUE (tenant_id, address),
        -- A classification other than unknown must say what it rests on and who decided it.
        CONSTRAINT ck_recipient_profiles_evidence CHECK (
            (legal_form = 'unknown' AND context = 'unknown') OR (evidence IS NOT NULL AND classified_by IS NOT NULL))
        """,
    )

    tenant_table(
        "regulatory_sources",
        """
        jurisdiction text NOT NULL,
        name text NOT NULL,
        version text NOT NULL,
        sha256 bytea NOT NULL,
        obtained_at timestamptz NOT NULL,
        effective_on date,
        expires_at timestamptz NOT NULL,
        entry_count integer NOT NULL CHECK (entry_count >= 0),
        obtained_how text NOT NULL,
        imported_by uuid,
        superseded_at timestamptz
        """,
        updated_at=False,
    )
    tenant_table(
        "regulatory_entries",
        f"""
        source_id uuid NOT NULL,
        address citext NOT NULL,
        {tenant_fk("regulatory_entries", "source_id", "regulatory_sources")}
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_regulatory_entries_address ON regulatory_entries (tenant_id, address)")

    tenant_table(
        "mailboxes",
        f"""
        provider text NOT NULL CHECK (provider IN ('gmail')),
        email_address citext NOT NULL,
        display_name text,
        owner_user_id uuid,
        mode text NOT NULL DEFAULT 'internal' CHECK (mode IN ('internal', 'external')),
        hosted_domain text,
        scopes text[] NOT NULL DEFAULT '{{}}',
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'degraded', 'error', 'revoked')),
        verification text NOT NULL DEFAULT 'implemented'
            CHECK (verification IN ('implemented', 'verified_locally', 'verified_in_sandbox', 'verified_live')),
        token_ciphertext bytea,
        token_nonce bytea,
        token_key_version text,
        history_cursor text,
        full_sync_required boolean NOT NULL DEFAULT true,
        full_sync_page_token text,
        full_sync_started_cursor text,
        watch_expires_at timestamptz,
        watch_renewed_at timestamptz,
        watch_renewal_failed_at timestamptz,
        last_synced_at timestamptz,
        last_notification_at timestamptz,
        last_error text,
        backoff_until timestamptz,
        consecutive_failures integer NOT NULL DEFAULT 0,
        alert text,
        sync_lease_token uuid,
        sync_lease_until timestamptz,
        {member_fk("mailboxes", "owner_user_id")},
        CONSTRAINT uq_mailboxes_address UNIQUE (tenant_id, provider, email_address)
        """,
    )

    # Push notifications arrive without a tenant. This table maps a mailbox address to its tenant
    # and is read only through mailbox_route(); like due_jobs it uses ENABLE, not FORCE.
    op.execute(
        """
        CREATE TABLE mailbox_routes (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            mailbox_id uuid NOT NULL,
            provider text NOT NULL,
            email_address citext NOT NULL,
            PRIMARY KEY (provider, email_address, tenant_id),
            CONSTRAINT fk_mailbox_routes_mailbox FOREIGN KEY (tenant_id, mailbox_id) REFERENCES mailboxes (tenant_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute("ALTER TABLE mailbox_routes ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mailbox_routes_tenant ON mailbox_routes FOR ALL TO crm_app
        USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE FUNCTION mailbox_route(p_provider text, p_address text) RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT r.tenant_id, r.mailbox_id FROM mailbox_routes r WHERE r.provider = p_provider AND r.email_address = p_address $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION mailbox_route(text, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION mailbox_route(text, text) TO crm_app")

    tenant_table(
        "email_threads",
        f"""
        mailbox_id uuid NOT NULL,
        provider_thread_id text,
        subject text,
        company_id uuid,
        lead_id uuid,
        contact_id uuid,
        link_state text NOT NULL DEFAULT 'unmatched' CHECK (link_state IN ('linked', 'unmatched', 'conflict', 'ignored')),
        link_note text,
        last_message_at timestamptz,
        has_inbound boolean NOT NULL DEFAULT false,
        {tenant_fk("email_threads", "mailbox_id", "mailboxes")},
        {tenant_fk("email_threads", "company_id", "companies", "SET NULL")},
        {tenant_fk("email_threads", "lead_id", "leads", "SET NULL")},
        {tenant_fk("email_threads", "contact_id", "contacts", "SET NULL")},
        CONSTRAINT uq_email_threads_provider UNIQUE (tenant_id, mailbox_id, provider_thread_id)
        """,
    )
    op.execute("CREATE INDEX ix_email_threads_lead ON email_threads (tenant_id, lead_id, last_message_at DESC)")
    op.execute(
        "CREATE INDEX ix_email_threads_review ON email_threads (tenant_id, link_state) WHERE link_state IN ('unmatched', 'conflict')"
    )

    tenant_table(
        "email_messages",
        f"""
        mailbox_id uuid NOT NULL,
        thread_id uuid NOT NULL,
        provider_message_id text NOT NULL,
        rfc_message_id text,
        in_reply_to text,
        reference_ids text[] NOT NULL DEFAULT '{{}}',
        direction text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
        from_address citext,
        to_addresses citext[] NOT NULL DEFAULT '{{}}',
        cc_addresses citext[] NOT NULL DEFAULT '{{}}',
        subject text,
        snippet text,
        body_text text,
        body_html text,
        has_remote_content boolean NOT NULL DEFAULT false,
        attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
        classification text NOT NULL DEFAULT 'message'
            CHECK (classification IN ('message', 'reply', 'out_of_office', 'bounce', 'auto_generated')),
        provider_labels text[] NOT NULL DEFAULT '{{}}',
        sent_at timestamptz,
        send_intent_id uuid,
        {tenant_fk("email_messages", "mailbox_id", "mailboxes")},
        {tenant_fk("email_messages", "thread_id", "email_threads")},
        CONSTRAINT uq_email_messages_provider UNIQUE (tenant_id, mailbox_id, provider_message_id)
        """,
        updated_at=False,
    )
    op.execute("CREATE INDEX ix_email_messages_thread ON email_messages (tenant_id, thread_id, sent_at)")
    op.execute(
        "CREATE INDEX ix_email_messages_rfc ON email_messages (tenant_id, rfc_message_id) WHERE rfc_message_id IS NOT NULL"
    )

    tenant_table(
        "email_drafts",
        f"""
        mailbox_id uuid,
        lead_id uuid,
        company_id uuid,
        contact_id uuid,
        thread_id uuid,
        reply_to_message_id uuid,
        kind text NOT NULL CHECK (kind IN ('unsolicited', 'reply', 'requested')),
        to_address citext NOT NULL,
        subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 300),
        body_text text NOT NULL CHECK (length(body_text) BETWEEN 1 AND 20000),
        language text NOT NULL DEFAULT 'bg',
        version integer NOT NULL DEFAULT 1,
        status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'queued', 'sent', 'cancelled')),
        approved_version integer,
        approved_content_hash text,
        approved_by uuid,
        approved_at timestamptz,
        created_by uuid,
        {tenant_fk("email_drafts", "mailbox_id", "mailboxes", "SET NULL")},
        {tenant_fk("email_drafts", "lead_id", "leads", "SET NULL")},
        {tenant_fk("email_drafts", "company_id", "companies", "SET NULL")},
        {tenant_fk("email_drafts", "contact_id", "contacts", "SET NULL")},
        {tenant_fk("email_drafts", "thread_id", "email_threads", "SET NULL")},
        {tenant_fk("email_drafts", "reply_to_message_id", "email_messages", "SET NULL")}
        """,
    )
    op.execute("CREATE INDEX ix_email_drafts_lead ON email_drafts (tenant_id, lead_id, created_at DESC)")

    tenant_table(
        "send_intents",
        f"""
        draft_id uuid NOT NULL,
        mailbox_id uuid NOT NULL,
        draft_version integer NOT NULL,
        content_hash text NOT NULL,
        to_address citext NOT NULL,
        kind text NOT NULL,
        scheduled_for timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'queued'
            CHECK (state IN ('queued', 'claimed', 'dispatching', 'provider_accepted', 'failed', 'cancelled', 'unknown', 'blocked')),
        state_reason text,
        lease_token uuid,
        lease_expires_at timestamptz,
        attempts integer NOT NULL DEFAULT 0,
        rfc_message_id text NOT NULL,
        provider_message_id text,
        provider_thread_id text,
        dispatched_at timestamptz,
        accepted_at timestamptz,
        delivery_evidence text NOT NULL DEFAULT 'none' CHECK (delivery_evidence IN ('none', 'bounced', 'replied')),
        requested_by uuid,
        dry_run boolean NOT NULL DEFAULT false,
        {tenant_fk("send_intents", "draft_id", "email_drafts")},
        {tenant_fk("send_intents", "mailbox_id", "mailboxes")},
        -- One send intent per approved version of a draft: the business action cannot be duplicated.
        CONSTRAINT uq_send_intents_draft_version UNIQUE (tenant_id, draft_id, draft_version),
        CONSTRAINT uq_send_intents_rfc UNIQUE (tenant_id, rfc_message_id)
        """,
    )
    op.execute("CREATE INDEX ix_send_intents_due ON send_intents (scheduled_for) WHERE state IN ('queued', 'claimed')")
    op.execute(
        "CREATE INDEX ix_send_intents_review ON send_intents (tenant_id, state) WHERE state IN ('unknown', 'failed', 'blocked')"
    )

    tenant_table(
        "eligibility_decisions",
        f"""
        draft_id uuid,
        send_intent_id uuid,
        to_address citext NOT NULL,
        kind text NOT NULL,
        stage text NOT NULL CHECK (stage IN ('preview', 'request', 'dispatch')),
        outcome text NOT NULL CHECK (outcome IN ('allow', 'review', 'block')),
        reasons jsonb NOT NULL,
        checks jsonb NOT NULL,
        policy_version text,
        content_hash text,
        decided_by uuid,
        {tenant_fk("eligibility_decisions", "draft_id", "email_drafts", "SET NULL")}
        """,
        updated_at=False,
    )
    op.execute(
        "CREATE INDEX ix_eligibility_decisions_draft ON eligibility_decisions (tenant_id, draft_id, created_at DESC)"
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON eligibility_decisions FROM crm_app")

    # Every tenant starts with the draft Bulgarian policy.
    op.execute(
        f"""
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN SELECT id FROM tenants LOOP
                PERFORM set_config('app.tenant_id', t.id::text, true);
                INSERT INTO outreach_policies (tenant_id, jurisdiction, version, rules, source_checked_on, source_note)
                VALUES (t.id, 'BG', 'bg-2026-10-draft', {DRAFT_POLICY_RULES}, DATE '2026-10-08',
                        'Read from the 2019 English consolidation; unofficial and possibly superseded.')
                ON CONFLICT DO NOTHING;
            END LOOP;
            PERFORM set_config('app.tenant_id', '', true);
        END $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION tenant_seed_outreach() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        DECLARE v_previous text := current_setting('app.tenant_id', true);
        BEGIN
            PERFORM set_config('app.tenant_id', NEW.id::text, true);
            INSERT INTO outreach_policies (tenant_id, jurisdiction, version, rules, source_checked_on, source_note)
            VALUES (NEW.id, 'BG', 'bg-2026-10-draft', {DRAFT_POLICY_RULES}, DATE '2026-10-08',
                    'Read from the 2019 English consolidation; unofficial and possibly superseded.');
            PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
            RETURN NEW;
        END $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION tenant_seed_outreach() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER tenants_seed_outreach AFTER INSERT ON tenants FOR EACH ROW EXECUTE FUNCTION tenant_seed_outreach()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tenants_seed_outreach ON tenants")
    op.execute("DROP FUNCTION IF EXISTS tenant_seed_outreach()")
    op.execute("DROP FUNCTION IF EXISTS mailbox_route(text, text)")
    op.execute("DROP TABLE IF EXISTS mailbox_routes")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS sender_identity")
