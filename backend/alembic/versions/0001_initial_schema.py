"""Initial FIRA schema (applies app/db/schema.sql; PostGIS layer when available).

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql((DB_DIR / "schema.sql").read_text(encoding="utf-8"))
    has_postgis = conn.execute(text("SELECT count(*) FROM pg_available_extensions WHERE name = 'postgis'")).scalar()
    if has_postgis:
        conn.exec_driver_sql((DB_DIR / "schema_postgis.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS dataset_manifest, scenario_labels, evaluation_runs, risk_config_versions,
            document_chunks, document_registry, users, audit_log, human_decisions, agent_episodes, evidence,
            investigations, alerts, transactions, devices, merchants, accounts, customer_identifiers, customers CASCADE
    """)
