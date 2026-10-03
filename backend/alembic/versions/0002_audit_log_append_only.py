"""Enforce append-only semantics on audit_log with triggers (app/db/schema_audit_guard.sql).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03
"""
from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"


def upgrade() -> None:
    op.get_bind().exec_driver_sql((DB_DIR / "schema_audit_guard.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql("""
        DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
        DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
        DROP FUNCTION IF EXISTS audit_log_reject_mutation();
    """)
