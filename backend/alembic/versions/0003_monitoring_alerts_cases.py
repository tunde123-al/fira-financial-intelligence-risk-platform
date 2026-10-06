"""Transaction monitoring, alerts and cases (app/db/schema_monitoring.sql).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""
from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"


def upgrade() -> None:
    op.get_bind().exec_driver_sql((DB_DIR / "schema_monitoring.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql("""
        DROP TABLE IF EXISTS case_evidence, case_notes, case_events, monitoring_alert_events,
            monitoring_alert_transactions, monitoring_alerts, cases, monitoring_runs CASCADE;
        DROP SEQUENCE IF EXISTS case_number_seq;
    """)
