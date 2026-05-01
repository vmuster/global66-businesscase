-- Global66 VoC Intelligence — SQLite schema
-- Documented in docs/02_data_pipeline.md §7

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS cases (
    case_id            TEXT PRIMARY KEY,
    pais_usuario       TEXT,
    country_iso        TEXT,
    language           TEXT,
    case_started_at    TIMESTAMP,
    synthetic_ts       INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    message_id         TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL REFERENCES cases(case_id),
    user_pseudonym     TEXT,
    direction          TEXT NOT NULL CHECK(direction IN ('INBOUND','OUTBOUND')),
    text               TEXT NOT NULL,
    language           TEXT,
    platform           TEXT NOT NULL DEFAULT 'unknown',
    created_at         TIMESTAMP NOT NULL,
    synthetic_ts       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_case ON messages(case_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS analyses (
    message_id         TEXT PRIMARY KEY REFERENCES messages(message_id),
    sentiment_score    REAL,
    sentiment_label    TEXT,
    primary_emotion    TEXT,
    weak_points        TEXT,           -- JSON array
    regulatory_flags   TEXT,           -- JSON array
    urgency_signals    TEXT,           -- JSON object
    escalation         TEXT,           -- JSON object
    confidence         REAL,
    score_base         REAL,
    score_final        REAL,
    priority           TEXT,
    analysis_status    TEXT NOT NULL,  -- ok | failed_validation | failed_llm
    model_used         TEXT,
    provider           TEXT,
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    analyzed_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analyses_score ON analyses(score_final DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_priority ON analyses(priority);

CREATE TABLE IF NOT EXISTS escalations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id         TEXT NOT NULL REFERENCES messages(message_id),
    case_id            TEXT NOT NULL,
    priority           TEXT NOT NULL,
    reason             TEXT,
    suggested_team     TEXT,
    payload_json       TEXT NOT NULL,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_escalations_case ON escalations(case_id);
CREATE INDEX IF NOT EXISTS idx_escalations_priority ON escalations(priority);
