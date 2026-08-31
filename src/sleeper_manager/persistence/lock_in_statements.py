"""Schema and guarded SQL for the live Lock-In opportunity table."""

LOCK_IN_OPPORTUNITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS lock_in_opportunities (
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
CREATE INDEX IF NOT EXISTS lock_in_opportunities_due_idx
ON lock_in_opportunities (status, next_check_at, action_deadline);
CREATE INDEX IF NOT EXISTS lock_in_opportunities_ack_idx
ON lock_in_opportunities (league_id, fantasy_week, status, roster_id);
"""

LOCK_IN_OPPORTUNITY_COLUMNS = """
league_id, fantasy_week, roster_id, player_id, game_id,
provider_player_id, scheduled_start, action_deadline, fantasy_week_end,
status, next_check_at, slot_index, slot_position, eligible_positions_json,
rostered_at_tipoff, roster_evidence_at, league_configuration_fingerprint,
current_observed_score, current_observation_fingerprint,
consecutive_direct_poll_count, current_poll_id, previous_observed_at,
current_observed_at, stable_score, stable_fingerprint, stabilized_at,
score_revision, current_recommendation_id, current_recommendation_kind,
acknowledged_action, acknowledged_at, latest_evaluation_hash, trace_json,
row_version, created_at, updated_at
"""

INSERT_LOCK_IN_OPPORTUNITY_SQL = f"""
INSERT OR IGNORE INTO lock_in_opportunities ({LOCK_IN_OPPORTUNITY_COLUMNS})
VALUES ({", ".join("?" for _ in range(36))})
"""

UPDATE_LOCK_IN_OPPORTUNITY_SQL = """
UPDATE lock_in_opportunities SET
    provider_player_id = ?, scheduled_start = ?, action_deadline = ?, fantasy_week_end = ?,
    status = ?, next_check_at = ?, slot_index = ?, slot_position = ?,
    eligible_positions_json = ?, rostered_at_tipoff = ?, roster_evidence_at = ?,
    league_configuration_fingerprint = ?, current_observed_score = ?,
    current_observation_fingerprint = ?, consecutive_direct_poll_count = ?,
    current_poll_id = ?, previous_observed_at = ?, current_observed_at = ?,
    stable_score = ?, stable_fingerprint = ?, stabilized_at = ?, score_revision = ?,
    current_recommendation_id = ?, current_recommendation_kind = ?,
    acknowledged_action = ?, acknowledged_at = ?, latest_evaluation_hash = ?,
    trace_json = ?, row_version = row_version + 1, updated_at = ?
WHERE league_id = ? AND fantasy_week = ? AND roster_id = ? AND player_id = ?
  AND game_id = ? AND row_version = ?
"""

LOAD_LOCK_IN_OPPORTUNITY_SQL = f"""
SELECT {LOCK_IN_OPPORTUNITY_COLUMNS} FROM lock_in_opportunities
WHERE league_id = ? AND fantasy_week = ? AND roster_id = ? AND player_id = ? AND game_id = ?
"""

RECORD_LOCK_IN_OBSERVATION_SQL = f"""
UPDATE lock_in_opportunities SET
    status = CASE
        WHEN status IN ('acknowledged_locked', 'acknowledged_passed') THEN status
        ELSE 'finalizing'
    END,
    previous_observed_at = current_observed_at,
    current_observed_score = ?, current_observation_fingerprint = ?,
    consecutive_direct_poll_count = CASE
        WHEN current_observation_fingerprint = ? THEN consecutive_direct_poll_count + 1 ELSE 1
    END,
    current_poll_id = ?, current_observed_at = ?, next_check_at = ?,
    stable_score = CASE
        WHEN current_observation_fingerprint = ? AND consecutive_direct_poll_count >= 1
            THEN ? ELSE stable_score END,
    stable_fingerprint = CASE
        WHEN current_observation_fingerprint = ? AND consecutive_direct_poll_count >= 1
            THEN ? ELSE stable_fingerprint END,
    stabilized_at = CASE
        WHEN current_observation_fingerprint = ? AND consecutive_direct_poll_count >= 1
            THEN ? ELSE stabilized_at END,
    score_revision = CASE
        WHEN current_observation_fingerprint = ? AND consecutive_direct_poll_count >= 1
             AND (stable_fingerprint IS NULL OR stable_fingerprint != ?)
            THEN score_revision + 1 ELSE score_revision END,
    row_version = row_version + 1, updated_at = ?
WHERE league_id = ? AND fantasy_week = ? AND roster_id = ? AND player_id = ?
  AND game_id = ? AND row_version = ? AND (current_poll_id IS NULL OR current_poll_id != ?)
RETURNING {LOCK_IN_OPPORTUNITY_COLUMNS}
"""

LIST_DUE_LOCK_IN_OPPORTUNITIES_SQL = f"""
SELECT {LOCK_IN_OPPORTUNITY_COLUMNS} FROM lock_in_opportunities
WHERE next_check_at <= ? AND action_deadline > ?
  AND status IN ('scheduled', 'active', 'finalizing', 'actionable', 'reconciliation_required')
ORDER BY next_check_at, league_id, fantasy_week, roster_id, player_id, game_id
LIMIT ?
"""

LIST_ACTIONABLE_LOCK_IN_OPPORTUNITIES_SQL = f"""
SELECT {LOCK_IN_OPPORTUNITY_COLUMNS} FROM lock_in_opportunities
WHERE league_id = ? AND fantasy_week = ? AND status = 'actionable'
ORDER BY action_deadline, roster_id, player_id, game_id
"""

EXPIRE_LOCK_IN_OPPORTUNITIES_SQL = """
UPDATE lock_in_opportunities SET status = 'expired', row_version = row_version + 1, updated_at = ?
WHERE action_deadline <= ?
  AND status IN ('scheduled', 'active', 'finalizing', 'actionable', 'reconciliation_required')
"""

LIST_ACKNOWLEDGED_LOCK_IN_OPPORTUNITIES_SQL = f"""
SELECT {LOCK_IN_OPPORTUNITY_COLUMNS} FROM lock_in_opportunities
WHERE league_id = ? AND fantasy_week = ?
  AND status IN ('acknowledged_locked', 'acknowledged_passed')
ORDER BY roster_id, player_id, scheduled_start, game_id
"""
