CREATE INDEX IF NOT EXISTS recommendations_league_week_decision_status_idx
ON recommendations (league_id, fantasy_week, decision_type, status);
