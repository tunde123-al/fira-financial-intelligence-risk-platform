"""Alert triage, data-quality accounting and configuration change log (app/db/schema_production.sql).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04
"""
from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"


def upgrade() -> None:
    op.get_bind().exec_driver_sql((DB_DIR / "schema_production.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql("""
        DROP TABLE IF EXISTS rejected_transactions, ingestion_batches, config_change_log CASCADE;
        DROP INDEX IF EXISTS ix_malerts_assignee_triage, ix_malerts_open_triage, ix_malerts_resolved_at,
            ix_cases_assigned_status;
        CREATE INDEX IF NOT EXISTS ix_cases_assigned ON cases (assigned_to);
        ALTER TABLE monitoring_alerts DROP COLUMN IF EXISTS triage_score, DROP COLUMN IF EXISTS triage_priority,
            DROP COLUMN IF EXISTS triage_factors, DROP COLUMN IF EXISTS triage_computed_at;
    """)
