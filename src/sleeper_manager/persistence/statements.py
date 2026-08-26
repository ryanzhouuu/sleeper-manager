"""Shared SQL for SQLite and D1 state repositories.

Schema strings stay backend-specific where CHECK constraints and bootstrap
migrations differ. DML is the contract both executors must keep in lockstep.
Lock-In evidence SELECT SQL stays in `acknowledgements`.
"""

from __future__ import annotations

from datetime import datetime

from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkStatus

D1_SCHEMA = """
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
    trace_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS recommendations_revision_idx
ON recommendations (league_id, fantasy_week, decision_type, revision);

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

CREATE INDEX IF NOT EXISTS recommendations_league_week_decision_status_idx
ON recommendations (league_id, fantasy_week, decision_type, status);

CREATE TABLE IF NOT EXISTS runtime_policy (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    version TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nba_cache (
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

CREATE TABLE IF NOT EXISTS projection_observations (
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

CREATE INDEX IF NOT EXISTS projection_observations_version_start_idx
ON projection_observations (history_version, game_start, player_id);

CREATE TABLE IF NOT EXISTS scheduled_work (
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

CREATE INDEX IF NOT EXISTS scheduled_work_due_idx
ON scheduled_work (status, due_at, lease_expires_at);
"""

SQLITE_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lock_acknowledgements (
    recommendation_id TEXT PRIMARY KEY,
    player_id TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS league_profiles (
    league_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
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
    trace_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
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
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
);
"""

SQLITE_REVISION_BACKFILL_SQL = """
UPDATE recommendations AS current
SET revision = (
    SELECT COUNT(*) FROM recommendations AS earlier
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
)
"""

SQLITE_RUNTIME_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS recommendations_revision_idx
ON recommendations (league_id, fantasy_week, decision_type, revision);
CREATE TABLE IF NOT EXISTS runtime_policy (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    version TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nba_cache (
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
CREATE TABLE IF NOT EXISTS projection_observations (
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
CREATE INDEX IF NOT EXISTS projection_observations_version_start_idx
ON projection_observations (history_version, game_start, player_id);
CREATE TABLE IF NOT EXISTS scheduled_work (
    work_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL,
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
    FOREIGN KEY (recommendation_id)
        REFERENCES recommendations(recommendation_id)
);
CREATE INDEX IF NOT EXISTS scheduled_work_due_idx
ON scheduled_work (status, due_at, lease_expires_at);
"""

UPSERT_LEAGUE_SNAPSHOT_SQL = """
INSERT INTO league_snapshots (
    snapshot_id, league_id, fantasy_week, payload_json, retrieved_at
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT(league_id, fantasy_week) DO UPDATE SET
    snapshot_id = excluded.snapshot_id,
    payload_json = excluded.payload_json,
    retrieved_at = excluded.retrieved_at
"""

LOAD_LEAGUE_SNAPSHOT_SQL = """
SELECT snapshot_id, league_id, fantasy_week, payload_json, retrieved_at
FROM league_snapshots WHERE league_id = ? AND fantasy_week = ?
"""

UPSERT_DATA_FRESHNESS_SQL = """
INSERT INTO data_freshness (
    resource, retrieved_at, expires_at, quality, warnings_json, errors_json
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(resource) DO UPDATE SET
    retrieved_at = excluded.retrieved_at,
    expires_at = excluded.expires_at,
    quality = excluded.quality,
    warnings_json = excluded.warnings_json,
    errors_json = excluded.errors_json
"""

LOAD_DATA_FRESHNESS_SQL = """
SELECT resource, retrieved_at, expires_at, quality, warnings_json, errors_json
FROM data_freshness WHERE resource = ?
"""

LOAD_LEAGUE_PROFILE_SQL = """
SELECT league_id, fingerprint, retrieved_at FROM league_profiles WHERE league_id = ?
"""

UPSERT_LEAGUE_PROFILE_SQL = """
INSERT INTO league_profiles (league_id, fingerprint, retrieved_at)
VALUES (?, ?, ?)
ON CONFLICT(league_id) DO UPDATE SET
    fingerprint = excluded.fingerprint,
    retrieved_at = excluded.retrieved_at
"""

INSERT_RECOMMENDATION_SQL = """
INSERT OR IGNORE INTO recommendations (
    recommendation_id, idempotency_key, league_id, fantasy_week,
    player_id, game_id, decision_type, title, message, deadline,
    policy_version, created_at, status, acknowledged_action,
    acknowledged_at, trace_json, revision
) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
         COALESCE(MAX(revision), 0) + 1
  FROM recommendations
  WHERE league_id = ? AND fantasy_week = ? AND decision_type = ?
"""

RECOMMENDATION_COLUMNS = """
recommendation_id, idempotency_key, league_id, fantasy_week,
player_id, game_id, decision_type, title, message, deadline,
policy_version, created_at, status, acknowledged_action,
acknowledged_at, trace_json, revision
"""

LOAD_RECOMMENDATION_SQL = f"""
SELECT {RECOMMENDATION_COLUMNS}
FROM recommendations WHERE recommendation_id = ?
"""

LIST_PENDING_RECOMMENDATIONS_SQL = f"""
SELECT {RECOMMENDATION_COLUMNS}
FROM recommendations
WHERE league_id = ? AND fantasy_week = ? AND decision_type = ? AND status = ?
ORDER BY created_at ASC, recommendation_id ASC
"""

INSERT_DELIVERY_ATTEMPT_SQL = """
INSERT INTO delivery_attempts (
    delivery_id, recommendation_id, provider, attempt_number,
    attempted_at, succeeded, error
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

NEXT_DELIVERY_ATTEMPT_SQL = """
SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number
FROM delivery_attempts
WHERE recommendation_id = ? AND provider != '_delivery_claim'
"""

HAS_SUCCESSFUL_DELIVERY_SQL = """
SELECT 1 AS found FROM delivery_attempts
WHERE recommendation_id = ? AND succeeded = 1 LIMIT 1
"""

CLAIM_DELIVERY_SQL = """
INSERT INTO delivery_attempts (
    delivery_id, recommendation_id, provider, attempt_number,
    attempted_at, succeeded, error
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(delivery_id) DO UPDATE SET
    attempted_at = excluded.attempted_at
WHERE delivery_attempts.provider = '_delivery_claim'
  AND delivery_attempts.succeeded = 0
  AND delivery_attempts.attempted_at <= ?
"""

RELEASE_DELIVERY_CLAIM_SQL = """
DELETE FROM delivery_attempts
WHERE delivery_id = ? AND succeeded = 0 AND provider = ?
"""

INSERT_ACTION_TOKEN_SQL = """
INSERT INTO action_tokens (
    token_hash, recommendation_id, action, created_at, expires_at, used_at
) VALUES (?, ?, ?, ?, ?, ?)
"""

INSERT_OR_IGNORE_ACTION_TOKEN_SQL = """
INSERT OR IGNORE INTO action_tokens (
    token_hash, recommendation_id, action, created_at, expires_at, used_at
) VALUES (?, ?, ?, ?, ?, ?)
"""

LOAD_ACTION_TOKEN_SQL = """
SELECT recommendation_id, action, expires_at, used_at
FROM action_tokens WHERE token_hash = ?
"""

LOAD_ACTION_TOKEN_ID_SQL = """
SELECT recommendation_id FROM action_tokens WHERE token_hash = ?
"""

CONSUME_ACKNOWLEDGEMENT_INSERT_SQL = """
INSERT OR IGNORE INTO acknowledgements (
    acknowledgement_id, recommendation_id, action, acknowledged_at, token_hash
)
SELECT ?, r.recommendation_id, ?, ?, t.token_hash
FROM action_tokens t
JOIN recommendations r ON r.recommendation_id = t.recommendation_id
WHERE t.token_hash = ? AND t.action = ? AND t.used_at IS NULL
  AND t.expires_at > ? AND r.status = ?
"""

CONSUME_TOKEN_MARK_USED_SQL = """
UPDATE action_tokens SET used_at = ?
WHERE token_hash = ? AND used_at IS NULL AND EXISTS (
    SELECT 1 FROM acknowledgements WHERE acknowledgement_id = ?
)
"""

CONSUME_RECOMMENDATION_ACK_SQL = """
UPDATE recommendations
SET status = ?, acknowledged_action = ?, acknowledged_at = ?
WHERE recommendation_id = ? AND status = ? AND EXISTS (
    SELECT 1 FROM acknowledgements WHERE acknowledgement_id = ?
)
"""

CONSUME_LOCK_ACK_SQL = """
INSERT OR IGNORE INTO lock_acknowledgements
    (recommendation_id, player_id, acknowledged_at)
SELECT r.recommendation_id, r.player_id, ?
FROM recommendations r
JOIN acknowledgements a ON a.recommendation_id = r.recommendation_id
WHERE a.acknowledgement_id = ? AND a.action = ?
"""

EXPIRE_RECOMMENDATIONS_SQL = """
UPDATE recommendations SET status = ?
WHERE status = ? AND deadline IS NOT NULL AND deadline <= ?
"""

SUPERSEDE_RECOMMENDATION_SQL = """
UPDATE recommendations SET status = ? WHERE recommendation_id = ? AND status = ?
"""

UPSERT_LOCK_ACKNOWLEDGEMENT_SQL = """
INSERT OR REPLACE INTO lock_acknowledgements
    (recommendation_id, player_id, acknowledged_at)
VALUES (?, ?, ?)
"""

IS_LOCKED_SQL = """
SELECT 1 AS found FROM lock_acknowledgements WHERE recommendation_id = ?
UNION ALL
SELECT 1 AS found FROM recommendations
WHERE recommendation_id = ? AND status = ? AND acknowledged_action = ?
LIMIT 1
"""

LOAD_RUNTIME_POLICY_SQL = """
SELECT version, payload_json, updated_at FROM runtime_policy WHERE singleton_id = 1
"""

UPSERT_RUNTIME_POLICY_SQL = """
INSERT INTO runtime_policy (singleton_id, version, payload_json, updated_at)
VALUES (1, ?, ?, ?)
ON CONFLICT(singleton_id) DO UPDATE SET
    version = excluded.version,
    payload_json = excluded.payload_json,
    updated_at = excluded.updated_at
"""

LOAD_NBA_CACHE_SQL = """
SELECT cache_key, provider, resource, schema_version, payload_json,
       retrieved_at, source_updated_at, expires_at, quality,
       warnings_json, errors_json
FROM nba_cache WHERE cache_key = ?
"""

UPSERT_NBA_CACHE_SQL = """
INSERT INTO nba_cache (
    cache_key, provider, resource, schema_version, payload_json,
    retrieved_at, source_updated_at, expires_at, quality,
    warnings_json, errors_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(cache_key) DO UPDATE SET
    provider = excluded.provider,
    resource = excluded.resource,
    schema_version = excluded.schema_version,
    payload_json = excluded.payload_json,
    retrieved_at = excluded.retrieved_at,
    source_updated_at = excluded.source_updated_at,
    expires_at = excluded.expires_at,
    quality = excluded.quality,
    warnings_json = excluded.warnings_json,
    errors_json = excluded.errors_json
"""

UPSERT_PROJECTION_OBSERVATION_SQL = """
INSERT INTO projection_observations (
    history_version, player_id, game_id, game_start,
    outcome_finalized_at, minutes, started, did_not_play,
    box_score_json, source_version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(history_version, player_id, game_id) DO UPDATE SET
    game_start = excluded.game_start,
    outcome_finalized_at = excluded.outcome_finalized_at,
    minutes = excluded.minutes,
    started = excluded.started,
    did_not_play = excluded.did_not_play,
    box_score_json = excluded.box_score_json,
    source_version = excluded.source_version
"""

LOAD_PROJECTION_OBSERVATIONS_SQL = """
SELECT history_version, player_id, game_id, game_start,
       outcome_finalized_at, minutes, started, did_not_play,
       box_score_json, source_version
FROM projection_observations
WHERE {where}
ORDER BY game_start, game_id, player_id
LIMIT ? OFFSET ?
"""

UPSERT_SCHEDULED_WORK_SQL = """
INSERT INTO scheduled_work (
    work_id, dedupe_key, kind, due_at, status, local_day, game_id,
    recommendation_id, deadline, lease_expires_at, attempt_count,
    correlation_id, failure_category, terminal_summary_json,
    created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(dedupe_key) DO UPDATE SET
    kind = excluded.kind,
    due_at = excluded.due_at,
    status = CASE
        WHEN scheduled_work.status IN ('completed', 'running')
            THEN scheduled_work.status
        ELSE excluded.status
    END,
    local_day = excluded.local_day,
    game_id = excluded.game_id,
    recommendation_id = excluded.recommendation_id,
    deadline = excluded.deadline,
    updated_at = excluded.updated_at
WHERE scheduled_work.status NOT IN ('completed', 'running')
"""

CLAIM_DUE_WORK_SQL = """
UPDATE scheduled_work
SET status = 'running', lease_expires_at = ?, attempt_count = attempt_count + 1,
    correlation_id = ?, failure_category = NULL,
    terminal_summary_json = NULL, updated_at = ?
WHERE work_id IN (
    SELECT work_id FROM scheduled_work
    WHERE due_at <= ? AND (
        status IN ('pending', 'retry')
        OR (status = 'running' AND lease_expires_at <= ?)
    )
    ORDER BY due_at, work_id
    LIMIT ?
)
RETURNING work_id, dedupe_key, kind, due_at, status, local_day,
          game_id, recommendation_id, deadline, lease_expires_at,
          attempt_count, correlation_id, failure_category,
          terminal_summary_json, created_at, updated_at
"""

FINISH_SCHEDULED_WORK_SQL = """
UPDATE scheduled_work
SET status = ?, due_at = COALESCE(?, due_at), lease_expires_at = NULL,
    failure_category = ?, terminal_summary_json = ?, updated_at = ?
WHERE work_id = ? AND status = 'running' AND correlation_id = ?
"""

LIST_SCHEDULED_WORK_SQL = """
SELECT work_id, dedupe_key, kind, due_at, status, local_day,
       game_id, recommendation_id, deadline, lease_expires_at,
       attempt_count, correlation_id, failure_category,
       terminal_summary_json, created_at, updated_at
FROM scheduled_work
{where}
ORDER BY due_at, work_id
"""

CANCEL_EXPIRED_SCHEDULED_WORK_SQL = """
UPDATE scheduled_work
SET status = 'canceled', lease_expires_at = NULL,
    failure_category = 'deadline_elapsed', updated_at = ?
WHERE deadline IS NOT NULL AND deadline <= ? AND (
    status IN ('pending', 'retry')
    OR (status = 'running' AND lease_expires_at <= ?)
)
"""

DELIVERY_CLAIM_PROVIDER = "_delivery_claim"


def projection_observation_filters(
    history_version: str,
    before: datetime | None,
) -> tuple[str, list[object]]:
    """Build the WHERE clause used by paged projection-history reads."""
    conditions = ["history_version = ?"]
    params: list[object] = [history_version]
    if before is not None:
        conditions.append("game_start < ?")
        conditions.append("outcome_finalized_at IS NOT NULL")
        conditions.append("outcome_finalized_at <= ?")
        params.extend((before.isoformat(), before.isoformat()))
    return " AND ".join(conditions), params


def scheduled_work_filters(
    kind: DueWorkKind | None,
    statuses: tuple[ScheduledWorkStatus, ...],
) -> tuple[str, list[object]]:
    """Build the optional WHERE clause for listing scheduled work."""
    conditions: list[str] = []
    params: list[object] = []
    if kind is not None:
        conditions.append("kind = ?")
        params.append(kind.value)
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(status.value for status in statuses)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params
