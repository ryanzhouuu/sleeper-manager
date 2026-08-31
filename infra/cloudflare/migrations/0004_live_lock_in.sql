PRAGMA defer_foreign_keys = ON;

CREATE TABLE scheduled_work_v4 (
    work_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (
        kind IN ('daily', 'pre_tipoff', 'delivery_retry', 'postgame')
    ),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'retry', 'completed', 'canceled')
    ),
    local_day TEXT,
    game_id TEXT,
    recommendation_id TEXT,
    deadline TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT,
    failure_category TEXT,
    terminal_summary_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
);

INSERT INTO scheduled_work_v4 (
    work_id, dedupe_key, kind, due_at, status, local_day, game_id,
    recommendation_id, deadline, lease_expires_at, attempt_count,
    correlation_id, failure_category, terminal_summary_json, created_at, updated_at
)
SELECT
    work_id, dedupe_key, kind, due_at, status, local_day, game_id,
    recommendation_id, deadline, lease_expires_at, attempt_count,
    correlation_id, failure_category, terminal_summary_json, created_at, updated_at
FROM scheduled_work;

DROP TABLE scheduled_work;
ALTER TABLE scheduled_work_v4 RENAME TO scheduled_work;

CREATE INDEX scheduled_work_due_idx
ON scheduled_work (status, due_at, lease_expires_at);

CREATE TABLE lock_in_opportunities (
    league_id TEXT NOT NULL,
    fantasy_week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    scheduled_start TEXT NOT NULL,
    action_deadline TEXT NOT NULL,
    fantasy_week_end TEXT NOT NULL,
    status TEXT NOT NULL,
    next_check_at TEXT NOT NULL,
    slot_index INTEGER,
    slot_position TEXT,
    eligible_positions_json TEXT NOT NULL,
    rostered_at_tipoff INTEGER,
    roster_evidence_at TEXT,
    league_configuration_fingerprint TEXT,
    current_observed_score REAL,
    current_observation_fingerprint TEXT,
    consecutive_direct_poll_count INTEGER NOT NULL DEFAULT 0,
    current_poll_id TEXT,
    previous_observed_at TEXT,
    current_observed_at TEXT,
    stable_score REAL,
    stable_fingerprint TEXT,
    stabilized_at TEXT,
    score_revision INTEGER NOT NULL DEFAULT 0,
    current_recommendation_id TEXT,
    current_recommendation_kind TEXT,
    acknowledged_action TEXT,
    acknowledged_at TEXT,
    latest_evaluation_hash TEXT,
    trace_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (league_id, fantasy_week, roster_id, player_id, game_id),
    FOREIGN KEY (current_recommendation_id) REFERENCES recommendations(recommendation_id)
);

CREATE INDEX lock_in_opportunities_due_idx
ON lock_in_opportunities (status, next_check_at, action_deadline);

CREATE INDEX lock_in_opportunities_ack_idx
ON lock_in_opportunities (league_id, fantasy_week, status, roster_id);
