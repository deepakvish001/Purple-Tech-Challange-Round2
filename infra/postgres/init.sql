-- Placeholder schema. Tables filled out by the aggregator slice.
-- Kept here so Postgres starts cleanly and a future migration tool can
-- diff against a known empty state.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS raw_events (
    id          BIGSERIAL PRIMARY KEY,
    event_id    UUID UNIQUE NOT NULL,
    type        TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    camera_id   TEXT,
    ts          TIMESTAMPTZ NOT NULL,
    session_id  TEXT,
    payload     JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raw_events_ts_idx        ON raw_events (ts);
CREATE INDEX IF NOT EXISTS raw_events_session_idx   ON raw_events (session_id);
CREATE INDEX IF NOT EXISTS raw_events_type_ts_idx   ON raw_events (type, ts);
