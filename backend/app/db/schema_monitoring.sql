-- Transaction monitoring, alert management and case management (Alembic revision 0003).
-- Requires revision 0001 (schema.sql) and revision 0002 (audit_log_reject_mutation()).
--
-- monitoring_alerts is deliberately separate from the legacy `alerts` table (the seeded upstream
-- legacy-rule feed read by HISTORICAL_ALERTS / NETWORK_EXPOSURE).

CREATE TABLE IF NOT EXISTS monitoring_runs (
    run_id               text PRIMARY KEY CHECK (run_id ~ '^RUN-[0-9A-F]+$'),
    started_at           timestamptz NOT NULL,
    finished_at          timestamptz,
    mode                 text NOT NULL DEFAULT 'batch',
    window_start         timestamptz,
    window_end           timestamptz,
    lookback_days        integer NOT NULL,
    config_version       text NOT NULL,
    config_fingerprint   text NOT NULL,
    customers_evaluated  integer NOT NULL DEFAULT 0,
    transactions_in_scope integer NOT NULL DEFAULT 0,
    alerts_created       integer NOT NULL DEFAULT 0,
    alerts_updated       integer NOT NULL DEFAULT 0,
    alerts_suppressed    integer NOT NULL DEFAULT 0,
    errors               integer NOT NULL DEFAULT 0,
    duration_ms          integer NOT NULL DEFAULT 0,
    triggered_by         text,
    details              jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_runs_started ON monitoring_runs (started_at DESC);

CREATE SEQUENCE IF NOT EXISTS case_number_seq;

CREATE TABLE IF NOT EXISTS cases (
    case_id          text PRIMARY KEY CHECK (case_id ~ '^CASE-[0-9A-F]+$'),
    case_number      text NOT NULL UNIQUE,
    customer_id      text NOT NULL REFERENCES customers (customer_id),
    status           text NOT NULL CHECK (status IN ('OPEN', 'INVESTIGATING', 'ESCALATED', 'PENDING_REVIEW', 'CLOSED')),
    priority         text NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    title            text NOT NULL,
    assigned_to      text,
    investigation_id text REFERENCES investigations (investigation_id),
    opened_at        timestamptz NOT NULL,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    closed_at        timestamptz,
    decision         text CHECK (decision IS NULL OR decision IN
                       ('CLEARED', 'FALSE_POSITIVE', 'CONFIRMED_SUSPICIOUS', 'ESCALATED')),
    decision_reason  text,
    decided_by       text,
    created_by       text,
    -- a closed case has a closing decision; an unclosed one never does
    CHECK ((status = 'CLOSED') = (closed_at IS NOT NULL)),
    CHECK (status <> 'CLOSED' OR (decision IS NOT NULL AND decision IN ('CLEARED', 'FALSE_POSITIVE', 'CONFIRMED_SUSPICIOUS')))
);
-- one unclosed case per customer: new alerts attach to it
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_open_case_per_customer ON cases (customer_id) WHERE status <> 'CLOSED';
CREATE INDEX IF NOT EXISTS ix_cases_status ON cases (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_cases_assigned ON cases (assigned_to);
CREATE INDEX IF NOT EXISTS ix_cases_customer ON cases (customer_id);

CREATE TABLE IF NOT EXISTS monitoring_alerts (
    alert_id          text PRIMARY KEY CHECK (alert_id ~ '^MAL-[0-9A-F]+$'),
    run_id            text REFERENCES monitoring_runs (run_id),
    customer_id       text NOT NULL REFERENCES customers (customer_id),
    account_id        text REFERENCES accounts (account_id),
    transaction_id    text REFERENCES transactions (transaction_id),
    detector_id       text NOT NULL,
    alert_type        text NOT NULL,
    category          text NOT NULL,
    severity          text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    risk_score        double precision NOT NULL,
    risk_contribution double precision NOT NULL,
    status            text NOT NULL CHECK (status IN ('NEW', 'TRIAGED', 'INVESTIGATING', 'ESCALATED', 'RESOLVED')),
    description       text NOT NULL,
    explanation       jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurrence_count  integer NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    window_start      timestamptz,
    window_end        timestamptz,
    triggered_at      timestamptz NOT NULL,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    last_seen_at      timestamptz NOT NULL,
    assigned_to       text,
    case_id           text REFERENCES cases (case_id),
    resolution        text CHECK (resolution IS NULL OR resolution IN ('CLEARED', 'FALSE_POSITIVE', 'CONFIRMED_SUSPICIOUS')),
    resolution_reason text,
    resolved_at       timestamptz,
    resolved_by       text,
    -- "escalated" is a status, never a resolution; a resolution exists exactly when the alert is resolved
    CHECK ((status = 'RESOLVED') = (resolution IS NOT NULL)),
    CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL))
);
-- deduplication policy: at most one unresolved alert per (customer, detector)
CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_unresolved_per_detector
    ON monitoring_alerts (customer_id, detector_id) WHERE status <> 'RESOLVED';
CREATE INDEX IF NOT EXISTS ix_malerts_status_triggered ON monitoring_alerts (status, triggered_at DESC);
CREATE INDEX IF NOT EXISTS ix_malerts_severity ON monitoring_alerts (severity);
CREATE INDEX IF NOT EXISTS ix_malerts_detector ON monitoring_alerts (detector_id);
CREATE INDEX IF NOT EXISTS ix_malerts_customer ON monitoring_alerts (customer_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS ix_malerts_assigned ON monitoring_alerts (assigned_to);
CREATE INDEX IF NOT EXISTS ix_malerts_risk ON monitoring_alerts (risk_score DESC);
CREATE INDEX IF NOT EXISTS ix_malerts_case ON monitoring_alerts (case_id);

-- the transactions that caused an alert
CREATE TABLE IF NOT EXISTS monitoring_alert_transactions (
    alert_id       text NOT NULL REFERENCES monitoring_alerts (alert_id) ON DELETE CASCADE,
    transaction_id text NOT NULL REFERENCES transactions (transaction_id),
    PRIMARY KEY (alert_id, transaction_id)
);
CREATE INDEX IF NOT EXISTS ix_mat_txn ON monitoring_alert_transactions (transaction_id);

CREATE TABLE IF NOT EXISTS monitoring_alert_events (
    id          bigserial PRIMARY KEY,
    alert_id    text NOT NULL REFERENCES monitoring_alerts (alert_id),
    ts          timestamptz NOT NULL,
    actor       text,
    event_type  text NOT NULL,
    from_status text,
    to_status   text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_mae_alert ON monitoring_alert_events (alert_id, ts);

CREATE TABLE IF NOT EXISTS case_events (
    id          bigserial PRIMARY KEY,
    case_id     text NOT NULL REFERENCES cases (case_id),
    ts          timestamptz NOT NULL,
    actor       text,
    event_type  text NOT NULL,
    from_status text,
    to_status   text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_ce_case ON case_events (case_id, ts);

CREATE TABLE IF NOT EXISTS case_notes (
    note_id    text PRIMARY KEY,
    case_id    text NOT NULL REFERENCES cases (case_id),
    author     text NOT NULL,
    body       text NOT NULL CHECK (length(btrim(body)) > 0),
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cn_case ON case_notes (case_id, created_at);

CREATE TABLE IF NOT EXISTS case_evidence (
    evidence_id    text PRIMARY KEY,
    case_id        text NOT NULL REFERENCES cases (case_id),
    alert_id       text REFERENCES monitoring_alerts (alert_id),
    transaction_id text REFERENCES transactions (transaction_id),
    document_id    text REFERENCES document_registry (document_id),
    chunk_id       text,
    evidence_class text NOT NULL CHECK (evidence_class IN
                     ('DATABASE_FACT', 'RULE_RESULT', 'GRAPH_RESULT', 'DOCUMENT_EVIDENCE', 'LLM_GENERATED_SUMMARY')),
    title          text NOT NULL,
    content        jsonb NOT NULL DEFAULT '{}'::jsonb,
    source         text NOT NULL,
    added_by       text NOT NULL,
    created_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cev_case ON case_evidence (case_id, created_at);

-- Append-only history: reuse the guard function from revision 0002 (database-enforced, not cryptographic).
DROP TRIGGER IF EXISTS mae_append_only ON monitoring_alert_events;
CREATE TRIGGER mae_append_only BEFORE UPDATE OR DELETE ON monitoring_alert_events
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
DROP TRIGGER IF EXISTS ce_append_only ON case_events;
CREATE TRIGGER ce_append_only BEFORE UPDATE OR DELETE ON case_events
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
DROP TRIGGER IF EXISTS cn_append_only ON case_notes;
CREATE TRIGGER cn_append_only BEFORE UPDATE OR DELETE ON case_notes
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
DROP TRIGGER IF EXISTS cev_append_only ON case_evidence;
CREATE TRIGGER cev_append_only BEFORE UPDATE OR DELETE ON case_evidence
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
