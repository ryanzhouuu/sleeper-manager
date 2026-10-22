CREATE TABLE manager_decision_changes (
    change_id TEXT PRIMARY KEY,
    write_id TEXT NOT NULL UNIQUE,
    league_id TEXT NOT NULL,
    fantasy_week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('not_locked', 'locked', 'passed')),
    previous_version INTEGER NOT NULL,
    previous_state_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX manager_decision_changes_scope_idx
ON manager_decision_changes (league_id, fantasy_week, roster_id, recorded_at DESC);
