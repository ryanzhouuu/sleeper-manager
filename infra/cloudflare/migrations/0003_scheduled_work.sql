ALTER TABLE recommendations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

UPDATE recommendations AS current
SET revision = (
    SELECT COUNT(*)
    FROM recommendations AS earlier
    WHERE earlier.league_id = current.league_id
      AND earlier.fantasy_week = current.fantasy_week
      AND earlier.decision_type = current.decision_type
      AND (
          earlier.created_at < current.created_at
          OR (
              earlier.created_at = current.created_at
              AND earlier.recommendation_id <= current.recommendation_id
          )
      )
);

CREATE UNIQUE INDEX recommendations_revision_idx
ON recommendations (league_id, fantasy_week, decision_type, revision);

CREATE TABLE runtime_policy (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    version TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE nba_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    resource TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_updated_at TEXT,
    expires_at TEXT,
    quality TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    errors_json TEXT NOT NULL
);

CREATE TABLE projection_observations (
    history_version TEXT NOT NULL,
    player_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    game_start TEXT NOT NULL,
    outcome_finalized_at TEXT,
    minutes REAL,
    started INTEGER NOT NULL,
    did_not_play INTEGER NOT NULL,
    box_score_json TEXT NOT NULL,
    source_version TEXT NOT NULL,
    PRIMARY KEY (history_version, player_id, game_id)
);

CREATE INDEX projection_observations_version_start_idx
ON projection_observations (history_version, game_start, player_id);

CREATE TABLE scheduled_work (
    work_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('daily', 'pre_tipoff', 'delivery_retry')),
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

CREATE INDEX scheduled_work_due_idx
ON scheduled_work (status, due_at, lease_expires_at);
