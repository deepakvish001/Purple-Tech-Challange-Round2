-- Full schema for the aggregator's reads + writes.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Raw event log (idempotent on event_id). Used as a debug / replay source.
CREATE TABLE IF NOT EXISTS raw_events (
    id          BIGSERIAL PRIMARY KEY,
    event_id    UUID UNIQUE NOT NULL,
    type        TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    camera_id   TEXT,
    ts          TIMESTAMPTZ NOT NULL,
    session_id  UUID,
    payload     JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS raw_events_ts_idx       ON raw_events (ts);
CREATE INDEX IF NOT EXISTS raw_events_type_ts_idx  ON raw_events (type, ts);
CREATE INDEX IF NOT EXISTS raw_events_session_idx  ON raw_events (session_id);


-- Customer / staff sessions stitched together by the aggregator.
CREATE TABLE IF NOT EXISTS sessions (
    session_id           UUID PRIMARY KEY,
    store_id             TEXT NOT NULL,
    embedding_id         TEXT,
    role                 TEXT NOT NULL DEFAULT 'unknown',
    entered_at           TIMESTAMPTZ NOT NULL,
    exited_at            TIMESTAMPTZ,
    funnel_stage         TEXT NOT NULL DEFAULT 'entered',
    checkout_at          TIMESTAMPTZ,
    receipt_invoice      TEXT,
    receipt_total        NUMERIC,
    receipt_items        INT,
    receipt_mode         TEXT,
    receipt_salesperson  TEXT
);
CREATE INDEX IF NOT EXISTS sessions_entered_at_idx ON sessions (entered_at);
CREATE INDEX IF NOT EXISTS sessions_embedding_idx  ON sessions (embedding_id);
CREATE INDEX IF NOT EXISTS sessions_stage_idx      ON sessions (funnel_stage);


-- One row per (session, zone) pair. Cumulative dwell across all visits.
CREATE TABLE IF NOT EXISTS session_zone_visits (
    id            BIGSERIAL PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    zone_id       TEXT NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL,
    last_seen     TIMESTAMPTZ NOT NULL,
    total_dwell_s NUMERIC NOT NULL DEFAULT 0,
    UNIQUE (session_id, zone_id)
);
CREATE INDEX IF NOT EXISTS szv_zone_idx ON session_zone_visits (zone_id);


-- Detected anomalies. Surfaced by /anomalies.
CREATE TABLE IF NOT EXISTS anomalies (
    id           BIGSERIAL PRIMARY KEY,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    bucket_ts    TIMESTAMPTZ,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'info',
    details      JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS anomalies_detected_idx ON anomalies (detected_at);


-- Hourly metrics rollup. Periodically refreshed by the aggregator.
-- /metrics reads from here; constant-time queries.
CREATE TABLE IF NOT EXISTS hourly_metrics (
    hour_bucket            TIMESTAMPTZ PRIMARY KEY,
    footfall               INT     NOT NULL DEFAULT 0,
    unique_visitors        INT     NOT NULL DEFAULT 0,
    purchases              INT     NOT NULL DEFAULT 0,
    checkouts              INT     NOT NULL DEFAULT 0,
    conversion_rate        NUMERIC NOT NULL DEFAULT 0,
    avg_session_duration_s NUMERIC,
    refreshed_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
