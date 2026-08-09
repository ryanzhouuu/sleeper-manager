PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS league_profiles (
    league_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    fantasy_week INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE (league_id, fantasy_week)
);

CREATE TABLE IF NOT EXISTS data_freshness (
    resource TEXT PRIMARY KEY,
    retrieved_at TEXT NOT NULL,
    expires_at TEXT,
    quality TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    errors_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    league_id TEXT NOT NULL,
    fantasy_week INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    game_id TEXT,
    decision_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    deadline TEXT,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    acknowledged_action TEXT,
    acknowledged_at TEXT,
    trace_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    delivery_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    attempted_at TEXT NOT NULL,
    succeeded INTEGER NOT NULL,
    error TEXT,
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
);

CREATE INDEX IF NOT EXISTS delivery_attempts_recommendation_idx
    ON delivery_attempts (recommendation_id, succeeded);

CREATE TABLE IF NOT EXISTS action_tokens (
    token_hash TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
);

CREATE TABLE IF NOT EXISTS acknowledgements (
    acknowledgement_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id),
    FOREIGN KEY (token_hash) REFERENCES action_tokens(token_hash)
);

CREATE TABLE IF NOT EXISTS lock_acknowledgements (
    recommendation_id TEXT PRIMARY KEY,
    player_id TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
