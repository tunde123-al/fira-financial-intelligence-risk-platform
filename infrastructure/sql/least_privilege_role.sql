-- Least-privilege PostgreSQL role for the FIRA API. Run as the database owner or a superuser, AFTER the migrations
-- (the API itself never needs to create or alter anything: `alembic upgrade head` runs as the owner).
--
--   psql "$OWNER_DATABASE_URL" -v app_password="'...a long random password...'" -f infrastructure/sql/least_privilege_role.sql
--
-- What the role CAN do : read every table; insert and update rows; delete only document chunks (document re-indexing).
-- What it CANNOT do    : create, alter or drop anything; truncate; delete rows elsewhere; update or delete rows in the
--                        append-only tables (audit log, alert/case history, ingestion ledger, quarantine, config change log)
--                        -- those triggers already refuse it, and the missing privilege is a second, independent layer.
-- Tested by backend/tests/integration/test_least_privilege_postgres.py.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fira_app') THEN
        CREATE ROLE fira_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;

ALTER ROLE fira_app PASSWORD :app_password;

REVOKE ALL ON SCHEMA public FROM fira_app;
GRANT USAGE ON SCHEMA public TO fira_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fira_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO fira_app;
GRANT DELETE ON document_chunks TO fira_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fira_app;

-- append-only tables: read and add, never change
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log, monitoring_alert_events, case_events, case_notes, case_evidence,
    ingestion_batches, rejected_transactions, config_change_log FROM fira_app;

-- the migration state is read (readiness check) but never written by the API
REVOKE INSERT, UPDATE ON alembic_version FROM fira_app;

-- tables created by later migrations get the same default treatment when the OWNER creates them
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO fira_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO fira_app;
