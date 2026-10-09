"""Let the application refresh planner statistics after a bulk import.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables an import fills, plus the ones the prospect list and call queue join against.
TABLES = (
    "companies, leads, lead_assessments, lead_scores, observations, hypotheses, contact_channels, "
    "contact_restrictions, import_rows, taggings, tasks, call_attempts"
)


def upgrade() -> None:
    # MAINTAIN allows ANALYZE (and nothing that changes data or structure). Without fresh statistics the
    # planner judges a workspace by the others in the database, and a newly imported large list is
    # queried with a plan meant for a nearly empty one.
    op.execute(f"GRANT MAINTAIN ON {TABLES} TO crm_app")


def downgrade() -> None:
    op.execute(f"REVOKE MAINTAIN ON {TABLES} FROM crm_app")
