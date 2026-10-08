"""Baseline: extensions and the helper that policies use to read the tenant.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    # Returns NULL when no tenant is set, so every policy comparison is false: fail closed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION app_current_tenant() TO crm_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")
