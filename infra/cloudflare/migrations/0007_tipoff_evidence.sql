PRAGMA defer_foreign_keys = ON;

ALTER TABLE lock_in_opportunities
ADD COLUMN tipoff_unverified INTEGER NOT NULL DEFAULT 0;

CREATE TABLE scheduled_work_v7 (
    work_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (
        kind IN ('daily', 'pre_tipoff', 'delivery_retry', 'postgame', 'tipoff_evidence')
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

INSERT INTO scheduled_work_v7 (
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
ALTER TABLE scheduled_work_v7 RENAME TO scheduled_work;

CREATE INDEX scheduled_work_due_idx
ON scheduled_work (status, due_at, lease_expires_at);

CREATE TABLE manager_decision_changes_v7 (
    change_id TEXT PRIMARY KEY,
    write_id TEXT NOT NULL UNIQUE,
    league_id TEXT NOT NULL,
    fantasy_week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN ('not_locked', 'locked', 'passed', 'not_lockable', 'lockable')
    ),
    previous_version INTEGER NOT NULL,
    previous_state_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

INSERT INTO manager_decision_changes_v7 SELECT * FROM manager_decision_changes;

DROP TABLE manager_decision_changes;
ALTER TABLE manager_decision_changes_v7 RENAME TO manager_decision_changes;

CREATE INDEX manager_decision_changes_scope_idx
ON manager_decision_changes (league_id, fantasy_week, roster_id, recorded_at DESC);
