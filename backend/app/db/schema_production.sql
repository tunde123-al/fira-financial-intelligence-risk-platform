-- Production-oriented additions (Alembic revision 0004): alert triage, data-quality accounting,
-- configuration change log. Requires revisions 0001 to 0003.

-- ---- alert triage (heuristic score and priority, with the factors that produced it)
ALTER TABLE monitoring_alerts ADD COLUMN IF NOT EXISTS triage_score double precision
    CHECK (triage_score IS NULL OR (triage_score >= 0 AND triage_score <= 100));
ALTER TABLE monitoring_alerts ADD COLUMN IF NOT EXISTS triage_priority text
    CHECK (triage_priority IS NULL OR triage_priority IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'));
ALTER TABLE monitoring_alerts ADD COLUMN IF NOT EXISTS triage_factors jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE monitoring_alerts ADD COLUMN IF NOT EXISTS triage_computed_at timestamptz;

-- Indexes added after measuring (infrastructure/scripts/index_review.py, docs/PERFORMANCE.md): each one is used by a query
-- the application issues and was timed before and after on 300,000 synthetic alerts. NULLS LAST matches the ORDER BY the
-- repository uses; a variant without it is not used by the planner. A non-partial triage index was 6x larger for no gain.
-- investigator queue: "my open alerts by triage score" and "highest-priority unresolved alerts"
CREATE INDEX IF NOT EXISTS ix_malerts_assignee_triage
    ON monitoring_alerts (assigned_to, triage_score DESC NULLS LAST) WHERE status <> 'RESOLVED';
CREATE INDEX IF NOT EXISTS ix_malerts_open_triage
    ON monitoring_alerts (triage_score DESC NULLS LAST) WHERE status <> 'RESOLVED';
-- "recently cleared / confirmed" lists and alert-quality windows
CREATE INDEX IF NOT EXISTS ix_malerts_resolved_at
    ON monitoring_alerts (resolved_at DESC) WHERE status = 'RESOLVED';
-- "my open cases"; its leftmost column also serves lookups by assignee, so the single-column index it supersedes is dropped
CREATE INDEX IF NOT EXISTS ix_cases_assigned_status ON cases (assigned_to, status);
DROP INDEX IF EXISTS ix_cases_assigned;

-- ---- data-quality accounting: one row per ingestion batch (written once, when the batch is finished)
CREATE TABLE IF NOT EXISTS ingestion_batches (
    batch_id        text PRIMARY KEY CHECK (batch_id ~ '^BAT-[0-9A-F]+$'),
    source          text NOT NULL,
    status          text NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz NOT NULL,
    created_by      text,
    expected_count  integer CHECK (expected_count IS NULL OR expected_count >= 0),
    received        integer NOT NULL CHECK (received >= 0),
    processed       integer NOT NULL CHECK (processed >= 0),
    rejected        integer NOT NULL CHECK (rejected >= 0),
    duplicates      integer NOT NULL DEFAULT 0 CHECK (duplicates >= 0),
    malformed       integer NOT NULL DEFAULT 0 CHECK (malformed >= 0),
    late            integer NOT NULL DEFAULT 0 CHECK (late >= 0),
    failed          integer NOT NULL DEFAULT 0 CHECK (failed >= 0),
    missing         integer CHECK (missing IS NULL OR missing >= 0),
    ids_generated   integer NOT NULL DEFAULT 0,
    customers_affected integer NOT NULL DEFAULT 0,
    error           text,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- every received row is processed, rejected or failed
    CHECK (received = processed + rejected + failed)
);
CREATE INDEX IF NOT EXISTS ix_batches_started ON ingestion_batches (started_at DESC);

-- quarantine: rows rejected by the data-quality gate, kept for drill-down (sanitised copy of the row)
CREATE TABLE IF NOT EXISTS rejected_transactions (
    id             bigserial PRIMARY KEY,
    batch_id       text NOT NULL REFERENCES ingestion_batches (batch_id),
    row_number     integer NOT NULL,
    transaction_id text,
    reason_code    text NOT NULL,
    reason_group   text NOT NULL CHECK (reason_group IN ('malformed', 'duplicate', 'invalid', 'referential')),
    reason         text NOT NULL,
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rejected_batch ON rejected_transactions (batch_id, row_number);
CREATE INDEX IF NOT EXISTS ix_rejected_reason ON rejected_transactions (reason_code, created_at DESC);

-- ---- configuration governance: who changed which threshold, from what, to what, and why
CREATE TABLE IF NOT EXISTS config_change_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL,
    config_name text NOT NULL,
    path        text NOT NULL,
    old_value   jsonb,
    new_value   jsonb,
    changed_by  text NOT NULL,
    reason      text,
    source      text NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cfglog_ts ON config_change_log (ts DESC);

-- the guard function now names the table it protects (same behaviour, accurate message for the new ledgers)
CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'insufficient_privilege',
        MESSAGE = TG_TABLE_NAME || ' is append-only: ' || TG_OP || ' is not permitted';
END;
$$;

DROP TRIGGER IF EXISTS ib_append_only ON ingestion_batches;
CREATE TRIGGER ib_append_only BEFORE UPDATE OR DELETE ON ingestion_batches
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
DROP TRIGGER IF EXISTS rt_append_only ON rejected_transactions;
CREATE TRIGGER rt_append_only BEFORE UPDATE OR DELETE ON rejected_transactions
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
DROP TRIGGER IF EXISTS ccl_append_only ON config_change_log;
CREATE TRIGGER ccl_append_only BEFORE UPDATE OR DELETE ON config_change_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
