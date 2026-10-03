-- FIRA relational schema (PostgreSQL 15+).
-- Single source of truth for DDL; applied by Alembic revision 0001.
-- Extensions that are optional (PostGIS) are handled in schema_postgis.sql.

CREATE TABLE IF NOT EXISTS customers (
    customer_id      text PRIMARY KEY CHECK (customer_id ~ '^CUST-[0-9]+$'),
    customer_type    text NOT NULL CHECK (customer_type IN ('individual', 'business')),
    segment          text NOT NULL,
    created_at       timestamptz NOT NULL,
    country          char(2) NOT NULL,
    home_city        text,
    home_latitude    double precision CHECK (home_latitude BETWEEN -90 AND 90),
    home_longitude   double precision CHECK (home_longitude BETWEEN -180 AND 180),
    risk_profile     text NOT NULL CHECK (risk_profile IN ('low', 'medium', 'high')),
    status           text NOT NULL CHECK (status IN ('active', 'dormant', 'closed', 'restricted')),
    kyc_level        smallint CHECK (kyc_level BETWEEN 1 AND 3)
);
CREATE INDEX IF NOT EXISTS ix_customers_segment ON customers (segment);

-- PII minimisation: only salted hashes (for matching) and masked display values are stored.
CREATE TABLE IF NOT EXISTS customer_identifiers (
    id               bigserial PRIMARY KEY,
    customer_id      text NOT NULL REFERENCES customers (customer_id),
    identifier_type  text NOT NULL CHECK (identifier_type IN ('phone', 'email', 'address')),
    value_hash       char(64) NOT NULL,
    masked_value     text NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_identifiers_hash ON customer_identifiers (identifier_type, value_hash);
CREATE INDEX IF NOT EXISTS ix_identifiers_customer ON customer_identifiers (customer_id);

CREATE TABLE IF NOT EXISTS accounts (
    account_id       text PRIMARY KEY CHECK (account_id ~ '^ACC-[0-9]+$'),
    customer_id      text NOT NULL REFERENCES customers (customer_id),
    account_type     text NOT NULL,
    currency         char(3) NOT NULL,
    opened_at        timestamptz NOT NULL,
    status           text NOT NULL,
    balance          numeric(18, 2) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_accounts_customer ON accounts (customer_id);

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id      text PRIMARY KEY CHECK (merchant_id ~ '^MER-[0-9]+$'),
    name             text NOT NULL,
    category         text NOT NULL,
    country          char(2) NOT NULL,
    city             text,
    latitude         double precision,
    longitude        double precision,
    risk_profile     text NOT NULL CHECK (risk_profile IN ('low', 'medium', 'high'))
);
CREATE INDEX IF NOT EXISTS ix_merchants_name ON merchants (lower(name));

CREATE TABLE IF NOT EXISTS devices (
    device_id        text PRIMARY KEY CHECK (device_id ~ '^DEV-[0-9]+$'),
    device_type      text NOT NULL,
    fingerprint      text NOT NULL,
    first_seen       timestamptz,
    last_seen        timestamptz
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id        text PRIMARY KEY CHECK (transaction_id ~ '^TXN-[0-9]+$'),
    "timestamp"           timestamptz NOT NULL,
    sender_account_id     text REFERENCES accounts (account_id),
    receiver_account_id   text REFERENCES accounts (account_id),
    merchant_id           text REFERENCES merchants (merchant_id),
    amount                numeric(18, 2) NOT NULL CHECK (amount > 0),
    currency              char(3) NOT NULL,
    amount_usd            numeric(18, 2),
    transaction_type      text NOT NULL,
    channel               text NOT NULL,
    country               char(2),
    latitude              double precision,
    longitude             double precision,
    device_id             text REFERENCES devices (device_id),
    ip_address            inet,
    status                text NOT NULL CHECK (status IN ('completed', 'failed', 'reversed', 'pending')),
    external_counterparty text,
    CHECK (sender_account_id IS NOT NULL OR receiver_account_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_tx_sender_ts ON transactions (sender_account_id, "timestamp");
CREATE INDEX IF NOT EXISTS ix_tx_receiver_ts ON transactions (receiver_account_id, "timestamp");
CREATE INDEX IF NOT EXISTS ix_tx_device_ts ON transactions (device_id, "timestamp");
CREATE INDEX IF NOT EXISTS ix_tx_merchant ON transactions (merchant_id);
CREATE INDEX IF NOT EXISTS ix_tx_ts ON transactions ("timestamp");

CREATE TABLE IF NOT EXISTS alerts (
    alert_id         text PRIMARY KEY,
    entity_id        text NOT NULL,
    entity_type      text NOT NULL,
    alert_type       text NOT NULL,
    severity         text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    risk_score       double precision,
    created_at       timestamptz NOT NULL,
    status           text NOT NULL,
    details          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_alerts_entity ON alerts (entity_id, created_at);
CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts (status);

CREATE TABLE IF NOT EXISTS investigations (
    investigation_id text PRIMARY KEY,
    subject_id       text NOT NULL,
    subject_type     text NOT NULL,
    assigned_to      text,
    status           text NOT NULL CHECK (status IN ('open', 'in_progress', 'pending_review', 'closed', 'needs_input', 'failed')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz,
    conclusion       text CHECK (conclusion IS NULL OR conclusion IN
                       ('confirmed_suspicious', 'legitimate', 'escalated', 'insufficient_evidence', 'more_evidence_requested')),
    request_text     text,
    risk_score       double precision,
    created_by       text,
    signals          text[] NOT NULL DEFAULT '{}',
    summary          text,
    report           jsonb
);
CREATE INDEX IF NOT EXISTS ix_investigations_subject ON investigations (subject_id);
CREATE INDEX IF NOT EXISTS ix_investigations_status ON investigations (status, created_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id      text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (investigation_id) ON DELETE CASCADE,
    ref              text NOT NULL,
    source_type      text NOT NULL CHECK (source_type IN ('database', 'metric', 'graph', 'document', 'ml', 'history')),
    source_id        text NOT NULL,
    evidence_type    text NOT NULL,
    title            text NOT NULL,
    content          jsonb NOT NULL,
    confidence       double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (investigation_id, ref)
);

-- Agent episodes store concise reasoning summaries and evidence references,
-- never raw model chain-of-thought.
CREATE TABLE IF NOT EXISTS agent_episodes (
    episode_id        text PRIMARY KEY,
    investigation_id  text REFERENCES investigations (investigation_id) ON DELETE SET NULL,
    request           text NOT NULL,
    requested_by      text,
    status            text NOT NULL,
    plan              jsonb NOT NULL DEFAULT '[]'::jsonb,
    tool_calls        jsonb NOT NULL DEFAULT '[]'::jsonb,
    observations      jsonb NOT NULL DEFAULT '[]'::jsonb,
    retrieved_evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    reasoning_summary text,
    final_output      jsonb,
    evaluation        jsonb,
    human_feedback    jsonb,
    model             text,
    tokens_in         integer NOT NULL DEFAULT 0,
    tokens_out        integer NOT NULL DEFAULT 0,
    latency_ms        integer,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);
CREATE INDEX IF NOT EXISTS ix_episodes_investigation ON agent_episodes (investigation_id);
CREATE INDEX IF NOT EXISTS ix_episodes_started ON agent_episodes (started_at DESC);

CREATE TABLE IF NOT EXISTS human_decisions (
    decision_id        text PRIMARY KEY,
    investigation_id   text NOT NULL REFERENCES investigations (investigation_id),
    decided_by         text NOT NULL,
    decision           text NOT NULL CHECK (decision IN ('confirm', 'reject', 'escalate', 'request_more_evidence')),
    rationale          text NOT NULL,
    failure_categories text[] NOT NULL DEFAULT '{}',
    report_quality     smallint CHECK (report_quality BETWEEN 1 AND 5),
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_decisions_investigation ON human_decisions (investigation_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    user_id      text,
    role         text,
    action       text NOT NULL,
    tool         text,
    entity_type  text,
    entity_id    text,
    result       text NOT NULL,
    request_id   text,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_user ON audit_log (user_id, ts DESC);
-- Append-only semantics are enforced by triggers added in Alembic revision 0002
-- (schema_audit_guard.sql). Not cryptographic immutability; see docs/SECURITY.md.

CREATE TABLE IF NOT EXISTS users (
    user_id        text PRIMARY KEY,
    username       text NOT NULL UNIQUE,
    password_hash  text NOT NULL,
    role           text NOT NULL CHECK (role IN ('analyst', 'admin')),
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_registry (
    document_id   text PRIMARY KEY,
    title         text NOT NULL,
    doc_type      text NOT NULL,
    source        text NOT NULL,
    jurisdiction  text,
    version       text,
    sha256        char(64) NOT NULL UNIQUE,
    page_count    integer,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id     text PRIMARY KEY,
    document_id  text NOT NULL REFERENCES document_registry (document_id) ON DELETE CASCADE,
    ordinal      integer NOT NULL,
    page         integer,
    section      text,
    text         text NOT NULL,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    tsv          tsvector GENERATED ALWAYS AS (
                     setweight(to_tsvector('english', coalesce(section, '')), 'A') ||
                     setweight(to_tsvector('english', text), 'B')) STORED
);
CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON document_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON document_chunks (document_id, ordinal);

CREATE TABLE IF NOT EXISTS risk_config_versions (
    version_id   text PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    status       text NOT NULL CHECK (status IN ('proposed', 'validated', 'rejected', 'active', 'retired')),
    rationale    text,
    config       jsonb NOT NULL,
    evaluation   jsonb,
    approved_by  text,
    approved_at  timestamptz
);
-- at most one active configuration
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_config ON risk_config_versions ((status)) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id         text PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    kind           text NOT NULL,
    config_version text,
    metrics        jsonb NOT NULL,
    details        jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Ground truth for the synthetic benchmark. Read ONLY by the evaluation module;
-- no agent tool exposes this table.
CREATE TABLE IF NOT EXISTS scenario_labels (
    entity_type      text NOT NULL,
    entity_id        text NOT NULL,
    scenario         text NOT NULL,
    is_suspicious    boolean NOT NULL,
    window_start     timestamptz,
    window_end       timestamptz,
    expected_signals text[] NOT NULL DEFAULT '{}',
    related_entities text[] NOT NULL DEFAULT '{}',
    notes            text,
    PRIMARY KEY (entity_type, entity_id, scenario)
);

CREATE TABLE IF NOT EXISTS dataset_manifest (
    id        smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    manifest  jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);
