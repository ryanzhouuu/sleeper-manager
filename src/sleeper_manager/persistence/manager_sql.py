"""Atomic correction statements use the inserted audit row as their write guard."""

INSERT_CHANGE = """
INSERT OR IGNORE INTO manager_decision_changes (
    change_id,write_id,league_id,fantasy_week,roster_id,player_id,game_id,action,
    previous_version,previous_state_json,recorded_at
)
SELECT ?,?,league_id,fantasy_week,roster_id,player_id,game_id,?,row_version,?,?
FROM lock_in_opportunities o
WHERE league_id=? AND fantasy_week=? AND roster_id=? AND player_id=? AND game_id=?
  AND row_version=?
  AND (? != 'locked' OR NOT EXISTS (
      SELECT 1 FROM lock_in_opportunities other
      WHERE other.league_id=o.league_id AND other.fantasy_week=o.fantasy_week
        AND other.roster_id=o.roster_id AND other.status='acknowledged_locked'
        AND (other.player_id!=o.player_id OR other.game_id!=o.game_id)
        AND (other.player_id=o.player_id OR other.slot_index=?)
  ))
"""

UPDATE_DECISION = """
UPDATE lock_in_opportunities SET status=?,slot_index=?,slot_position=?,
    acknowledged_action=?,acknowledged_at=?,next_check_at=?,latest_evaluation_hash=NULL,
    row_version=row_version+1,updated_at=?
WHERE league_id=? AND fantasy_week=? AND roster_id=? AND player_id=? AND game_id=?
  AND row_version=? AND EXISTS (
      SELECT 1 FROM manager_decision_changes WHERE change_id=? AND write_id=?
  )
"""

SUPERSEDE_DECISIONS = """
UPDATE recommendations SET status='superseded'
WHERE league_id=? AND fantasy_week=?
  AND ((player_id=? AND game_id=?) OR decision_type='weekly_lineup')
  AND EXISTS (SELECT 1 FROM manager_decision_changes WHERE change_id=? AND write_id=?)
"""

EXPIRE_OLD_TOKENS = """
UPDATE action_tokens SET expires_at=MIN(expires_at,?)
WHERE recommendation_id IN (
    SELECT recommendation_id FROM recommendations
    WHERE league_id=? AND fantasy_week=? AND player_id=? AND game_id=?
) AND EXISTS (SELECT 1 FROM manager_decision_changes WHERE change_id=? AND write_id=?)
"""

REMOVE_LOCK_INDEX = """
DELETE FROM lock_acknowledgements WHERE recommendation_id IN (
    SELECT recommendation_id FROM recommendations
    WHERE league_id=? AND fantasy_week=? AND player_id=? AND game_id=?
) AND EXISTS (SELECT 1 FROM manager_decision_changes WHERE change_id=? AND write_id=?)
"""

INSERT_REPLAN = """
INSERT OR IGNORE INTO scheduled_work (
    work_id,dedupe_key,kind,due_at,status,deadline,created_at,updated_at
)
SELECT ?,?,'daily',?,'pending',?,?,?
WHERE EXISTS (SELECT 1 FROM manager_decision_changes WHERE change_id=? AND write_id=?)
"""
