import json
import sqlite3
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.planning import AcknowledgedDecisionEvidence
from sleeper_manager.persistence.acknowledgements import (
    ACKNOWLEDGED_DECISIONS_INDEX_SQL,
    ACKNOWLEDGED_DECISIONS_QUERY,
    LOCK_IN_DECISION_TYPE,
    decode_acknowledged_decisions,
    raw_row_from_sequence,
)
from sleeper_manager.persistence.base import (
    DEFAULT_SCHEDULED_WORK_LEASE,
    PROJECTION_OBSERVATION_PAGE_SIZE,
    AcknowledgementAction,
    AcknowledgementOutcome,
    AcknowledgementResult,
    ActionTokenRecord,
    CachedNBARecord,
    DataFreshnessRecord,
    DeliveryAttemptRecord,
    DueWorkKind,
    LeagueSnapshotRecord,
    ProjectionObservationRecord,
    RecommendationRecord,
    RecommendationStatus,
    RuntimePolicyRecord,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
    StoredLeagueProfile,
)


class SQLiteStateRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lock_acknowledgements (
                    recommendation_id TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS league_profiles (
                    league_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
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
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS league_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    league_id TEXT NOT NULL,
                    fantasy_week INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    UNIQUE (league_id, fantasy_week)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS data_freshness (
                    resource TEXT PRIMARY KEY,
                    retrieved_at TEXT NOT NULL,
                    expires_at TEXT,
                    quality TEXT NOT NULL,
                    warnings_json TEXT NOT NULL,
                    errors_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    delivery_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    attempted_at TEXT NOT NULL,
                    succeeded INTEGER NOT NULL,
                    error TEXT,
                    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS action_tokens (
                    token_hash TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS acknowledgements (
                    acknowledgement_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
                )
                """
            )
            connection.execute(ACKNOWLEDGED_DECISIONS_INDEX_SQL)
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(recommendations)")
            }
            if "revision" not in columns:
                connection.execute(
                    "ALTER TABLE recommendations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    """
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
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS recommendations_revision_idx
                ON recommendations (league_id, fantasy_week, decision_type, revision)
                """
            )
            connection.executescript(
                """
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
            )

    def load_acknowledged_decisions(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        as_of: datetime,
    ) -> tuple[AcknowledgedDecisionEvidence, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                ACKNOWLEDGED_DECISIONS_QUERY,
                (league_id, fantasy_week, LOCK_IN_DECISION_TYPE),
            ).fetchall()
        return decode_acknowledged_decisions(
            tuple(raw_row_from_sequence(row) for row in rows),
            as_of=as_of,
        )

    @staticmethod
    def _recommendation(row: tuple[object, ...]) -> RecommendationRecord:
        return RecommendationRecord(
            recommendation_id=str(row[0]),
            idempotency_key=str(row[1]),
            league_id=str(row[2]),
            fantasy_week=int(str(row[3])),
            player_id=str(row[4]),
            game_id=str(row[5]) if row[5] is not None else None,
            decision_type=str(row[6]),
            title=str(row[7]),
            message=str(row[8]),
            deadline=datetime.fromisoformat(str(row[9])) if row[9] else None,
            policy_version=str(row[10]),
            created_at=datetime.fromisoformat(str(row[11])),
            status=RecommendationStatus(str(row[12])),
            acknowledged_action=(
                AcknowledgementAction(str(row[13])) if row[13] is not None else None
            ),
            acknowledged_at=datetime.fromisoformat(str(row[14])) if row[14] else None,
            trace_json=str(row[15]),
            revision=int(str(row[16])),
        )

    def load_profile(self, league_id: str) -> StoredLeagueProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT league_id, fingerprint, retrieved_at
                FROM league_profiles
                WHERE league_id = ?
                """,
                (league_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredLeagueProfile(
            league_id=row[0],
            fingerprint=row[1],
            retrieved_at=datetime.fromisoformat(row[2]),
        )

    def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO league_snapshots (
                    snapshot_id, league_id, fantasy_week, payload_json, retrieved_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(league_id, fantasy_week) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    payload_json = excluded.payload_json,
                    retrieved_at = excluded.retrieved_at
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.league_id,
                    snapshot.fantasy_week,
                    snapshot.payload_json,
                    snapshot.retrieved_at.isoformat(),
                ),
            )

    def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, league_id, fantasy_week, payload_json, retrieved_at
                FROM league_snapshots
                WHERE league_id = ? AND fantasy_week = ?
                """,
                (league_id, fantasy_week),
            ).fetchone()
        if row is None:
            return None
        return LeagueSnapshotRecord(
            snapshot_id=str(row[0]),
            league_id=str(row[1]),
            fantasy_week=int(str(row[2])),
            payload_json=str(row[3]),
            retrieved_at=datetime.fromisoformat(str(row[4])),
        )

    def save_data_freshness(self, freshness: DataFreshnessRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO data_freshness (
                    resource, retrieved_at, expires_at, quality, warnings_json, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource) DO UPDATE SET
                    retrieved_at = excluded.retrieved_at,
                    expires_at = excluded.expires_at,
                    quality = excluded.quality,
                    warnings_json = excluded.warnings_json,
                    errors_json = excluded.errors_json
                """,
                (
                    freshness.resource,
                    freshness.retrieved_at.isoformat(),
                    freshness.expires_at.isoformat() if freshness.expires_at is not None else None,
                    freshness.quality.value,
                    json.dumps(freshness.warnings),
                    json.dumps(freshness.errors),
                ),
            )

    def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resource, retrieved_at, expires_at, quality, warnings_json, errors_json
                FROM data_freshness
                WHERE resource = ?
                """,
                (resource,),
            ).fetchone()
        if row is None:
            return None
        return DataFreshnessRecord(
            resource=str(row[0]),
            retrieved_at=datetime.fromisoformat(str(row[1])),
            expires_at=datetime.fromisoformat(str(row[2])) if row[2] else None,
            quality=DataQualityState(str(row[3])),
            warnings=tuple(json.loads(str(row[4]))),
            errors=tuple(json.loads(str(row[5]))),
        )

    def save_profile(self, profile: StoredLeagueProfile) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO league_profiles (league_id, fingerprint, retrieved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(league_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    retrieved_at = excluded.retrieved_at
                """,
                (profile.league_id, profile.fingerprint, profile.retrieved_at.isoformat()),
            )

    def create_recommendation(self, recommendation: RecommendationRecord) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO recommendations (
                    recommendation_id, idempotency_key, league_id, fantasy_week,
                    player_id, game_id, decision_type, title, message, deadline,
                    policy_version, created_at, status, acknowledged_action,
                    acknowledged_at, trace_json, revision
                ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         COALESCE(MAX(revision), 0) + 1
                  FROM recommendations
                  WHERE league_id = ? AND fantasy_week = ? AND decision_type = ?
                """,
                (
                    recommendation.recommendation_id,
                    recommendation.idempotency_key,
                    recommendation.league_id,
                    recommendation.fantasy_week,
                    recommendation.player_id,
                    recommendation.game_id,
                    recommendation.decision_type,
                    recommendation.title,
                    recommendation.message,
                    recommendation.deadline.isoformat()
                    if recommendation.deadline is not None
                    else None,
                    recommendation.policy_version,
                    recommendation.created_at.isoformat(),
                    recommendation.status.value,
                    recommendation.acknowledged_action.value
                    if recommendation.acknowledged_action is not None
                    else None,
                    recommendation.acknowledged_at.isoformat()
                    if recommendation.acknowledged_at is not None
                    else None,
                    recommendation.trace_json,
                    recommendation.league_id,
                    recommendation.fantasy_week,
                    recommendation.decision_type,
                ),
            )
        return cursor.rowcount == 1

    def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recommendation_id, idempotency_key, league_id, fantasy_week,
                       player_id, game_id, decision_type, title, message, deadline,
                       policy_version, created_at, status, acknowledged_action,
                       acknowledged_at, trace_json, revision
                FROM recommendations
                WHERE recommendation_id = ?
                """,
                (recommendation_id,),
            ).fetchone()
        return self._recommendation(row) if row is not None else None

    def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_attempts (
                    delivery_id, recommendation_id, provider, attempt_number,
                    attempted_at, succeeded, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.delivery_id,
                    attempt.recommendation_id,
                    attempt.provider,
                    attempt.attempt_number,
                    attempt.attempted_at.isoformat(),
                    int(attempt.succeeded),
                    attempt.error,
                ),
            )

    def next_delivery_attempt_number(self, recommendation_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM delivery_attempts
                WHERE recommendation_id = ? AND provider != '_delivery_claim'
                """,
                (recommendation_id,),
            ).fetchone()
        return int(row[0])

    def has_successful_delivery(self, recommendation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM delivery_attempts
                WHERE recommendation_id = ? AND succeeded = 1
                LIMIT 1
                """,
                (recommendation_id,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _delivery_claim_id(recommendation_id: str) -> str:
        return sha256(f"{recommendation_id}:delivery-claim".encode()).hexdigest()

    def claim_delivery(
        self,
        recommendation_id: str,
        claimed_at: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> bool:
        reclaim_before = claimed_at - lease_duration
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO delivery_attempts (
                    delivery_id, recommendation_id, provider, attempt_number,
                    attempted_at, succeeded, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    attempted_at = excluded.attempted_at
                WHERE delivery_attempts.provider = '_delivery_claim'
                  AND delivery_attempts.succeeded = 0
                  AND delivery_attempts.attempted_at <= ?
                """,
                (
                    self._delivery_claim_id(recommendation_id),
                    recommendation_id,
                    "_delivery_claim",
                    0,
                    claimed_at.isoformat(),
                    0,
                    None,
                    reclaim_before.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def release_delivery_claim(self, recommendation_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM delivery_attempts
                WHERE delivery_id = ? AND succeeded = 0 AND provider = ?
                """,
                (self._delivery_claim_id(recommendation_id), "_delivery_claim"),
            )
        return cursor.rowcount == 1

    def create_action_token(self, token: ActionTokenRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO action_tokens (
                    token_hash, recommendation_id, action, created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token.token_hash,
                    token.recommendation_id,
                    token.action.value,
                    token.created_at.isoformat(),
                    token.expires_at.isoformat(),
                    token.used_at.isoformat() if token.used_at is not None else None,
                ),
            )

    def consume_action_token(
        self,
        token_hash: str,
        action: AcknowledgementAction,
        acknowledged_at: datetime,
    ) -> AcknowledgementResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = connection.execute(
                """
                SELECT recommendation_id, action, expires_at, used_at
                FROM action_tokens
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if token is None:
                return AcknowledgementResult(AcknowledgementOutcome.INVALID)
            if token[3] is not None:
                return AcknowledgementResult(AcknowledgementOutcome.ALREADY_USED)
            if datetime.fromisoformat(str(token[2])) <= acknowledged_at:
                return AcknowledgementResult(AcknowledgementOutcome.EXPIRED)
            if str(token[1]) != action.value:
                return AcknowledgementResult(AcknowledgementOutcome.CONFLICT)

            recommendation_row = connection.execute(
                """
                SELECT recommendation_id, idempotency_key, league_id, fantasy_week,
                       player_id, game_id, decision_type, title, message, deadline,
                       policy_version, created_at, status, acknowledged_action,
                       acknowledged_at, trace_json, revision
                FROM recommendations
                WHERE recommendation_id = ?
                """,
                (token[0],),
            ).fetchone()
            if recommendation_row is None:
                return AcknowledgementResult(AcknowledgementOutcome.INVALID)
            recommendation = self._recommendation(recommendation_row)
            if recommendation.status is RecommendationStatus.ACKNOWLEDGED:
                return AcknowledgementResult(
                    AcknowledgementOutcome.ALREADY_USED,
                    recommendation,
                )
            if recommendation.status is not RecommendationStatus.PENDING:
                return AcknowledgementResult(AcknowledgementOutcome.CONFLICT, recommendation)

            connection.execute(
                "UPDATE action_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (acknowledged_at.isoformat(), token_hash),
            )
            connection.execute(
                """
                UPDATE recommendations
                SET status = ?, acknowledged_action = ?, acknowledged_at = ?
                WHERE recommendation_id = ?
                """,
                (
                    RecommendationStatus.ACKNOWLEDGED.value,
                    action.value,
                    acknowledged_at.isoformat(),
                    recommendation.recommendation_id,
                ),
            )
            acknowledgement_id = sha256(
                f"{recommendation.recommendation_id}:{token_hash}".encode()
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO acknowledgements (
                    acknowledgement_id, recommendation_id, action, acknowledged_at, token_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    acknowledgement_id,
                    recommendation.recommendation_id,
                    action.value,
                    acknowledged_at.isoformat(),
                    token_hash,
                ),
            )
            if action is AcknowledgementAction.LOCKED:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO lock_acknowledgements
                        (recommendation_id, player_id, acknowledged_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        recommendation.recommendation_id,
                        recommendation.player_id,
                        acknowledged_at.isoformat(),
                    ),
                )
            updated = connection.execute(
                """
                SELECT recommendation_id, idempotency_key, league_id, fantasy_week,
                       player_id, game_id, decision_type, title, message, deadline,
                       policy_version, created_at, status, acknowledged_action,
                       acknowledged_at, trace_json, revision
                FROM recommendations
                WHERE recommendation_id = ?
                """,
                (recommendation.recommendation_id,),
            ).fetchone()
        return AcknowledgementResult(
            AcknowledgementOutcome.APPLIED,
            self._recommendation(updated) if updated is not None else None,
        )

    def expire_recommendations(self, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recommendations
                SET status = ?
                WHERE status = ? AND deadline IS NOT NULL AND deadline <= ?
                """,
                (
                    RecommendationStatus.EXPIRED.value,
                    RecommendationStatus.PENDING.value,
                    now.isoformat(),
                ),
            )
        return cursor.rowcount

    def list_pending_recommendations(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        decision_type: str,
    ) -> tuple[RecommendationRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT recommendation_id, idempotency_key, league_id, fantasy_week,
                       player_id, game_id, decision_type, title, message, deadline,
                       policy_version, created_at, status, acknowledged_action,
                       acknowledged_at, trace_json, revision
                FROM recommendations
                WHERE league_id = ?
                  AND fantasy_week = ?
                  AND decision_type = ?
                  AND status = ?
                ORDER BY created_at ASC, recommendation_id ASC
                """,
                (
                    league_id,
                    fantasy_week,
                    decision_type,
                    RecommendationStatus.PENDING.value,
                ),
            ).fetchall()
        return tuple(self._recommendation(row) for row in rows)

    def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool:
        del now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recommendations
                SET status = ?
                WHERE recommendation_id = ? AND status = ?
                """,
                (
                    RecommendationStatus.SUPERSEDED.value,
                    recommendation_id,
                    RecommendationStatus.PENDING.value,
                ),
            )
        return cursor.rowcount == 1

    def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO lock_acknowledgements
                    (recommendation_id, player_id, acknowledged_at)
                VALUES (?, ?, ?)
                """,
                (recommendation_id, player_id, acknowledged_at.isoformat()),
            )

    def is_locked(self, recommendation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM lock_acknowledgements WHERE recommendation_id = ?
                UNION ALL
                SELECT 1 FROM recommendations
                WHERE recommendation_id = ?
                  AND status = ?
                  AND acknowledged_action = ?
                LIMIT 1
                """,
                (
                    recommendation_id,
                    recommendation_id,
                    RecommendationStatus.ACKNOWLEDGED.value,
                    AcknowledgementAction.LOCKED.value,
                ),
            ).fetchone()
        return row is not None

    def load_runtime_policy(self) -> RuntimePolicyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT version, payload_json, updated_at
                FROM runtime_policy WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return RuntimePolicyRecord(str(row[0]), str(row[1]), datetime.fromisoformat(str(row[2])))

    def save_runtime_policy(self, policy: RuntimePolicyRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_policy (singleton_id, version, payload_json, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    version = excluded.version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (policy.version, policy.payload_json, policy.updated_at.isoformat()),
            )

    def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cache_key, provider, resource, schema_version, payload_json,
                       retrieved_at, source_updated_at, expires_at, quality,
                       warnings_json, errors_json
                FROM nba_cache WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        record = CachedNBARecord(
            cache_key=str(row[0]),
            provider=str(row[1]),
            resource=str(row[2]),
            schema_version=str(row[3]),
            payload_json=str(row[4]),
            retrieved_at=datetime.fromisoformat(str(row[5])),
            source_updated_at=datetime.fromisoformat(str(row[6])) if row[6] else None,
            expires_at=datetime.fromisoformat(str(row[7])) if row[7] else None,
            quality=DataQualityState(str(row[8])),
            warnings=tuple(json.loads(str(row[9]))),
            errors=tuple(json.loads(str(row[10]))),
        )
        if record.expires_at is not None and record.expires_at <= now:
            return CachedNBARecord(
                cache_key=record.cache_key,
                provider=record.provider,
                resource=record.resource,
                schema_version=record.schema_version,
                payload_json=record.payload_json,
                retrieved_at=record.retrieved_at,
                source_updated_at=record.source_updated_at,
                expires_at=record.expires_at,
                quality=DataQualityState.STALE,
                warnings=record.warnings + ("Cached record has exceeded its freshness window",),
                errors=record.errors,
            )
        return record

    def put(self, record: CachedNBARecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
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
                """,
                (
                    record.cache_key,
                    record.provider,
                    record.resource,
                    record.schema_version,
                    record.payload_json,
                    record.retrieved_at.isoformat(),
                    record.source_updated_at.isoformat() if record.source_updated_at else None,
                    record.expires_at.isoformat() if record.expires_at else None,
                    record.quality.value,
                    json.dumps(record.warnings),
                    json.dumps(record.errors),
                ),
            )

    def save_projection_observations(
        self, observations: tuple[ProjectionObservationRecord, ...]
    ) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
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
                """,
                tuple(
                    (
                        item.history_version,
                        item.player_id,
                        item.game_id,
                        item.game_start.isoformat(),
                        item.outcome_finalized_at.isoformat()
                        if item.outcome_finalized_at
                        else None,
                        item.minutes,
                        int(item.started),
                        int(item.did_not_play),
                        item.box_score_json,
                        item.source_version,
                    )
                    for item in observations
                ),
            )

    def load_projection_observations(
        self,
        history_version: str,
        *,
        before: datetime | None = None,
        page_size: int = PROJECTION_OBSERVATION_PAGE_SIZE,
    ) -> tuple[ProjectionObservationRecord, ...]:
        if page_size <= 0:
            raise ValueError("Projection observation page size must be positive")
        conditions = ["history_version = ?"]
        params: list[object] = [history_version]
        if before is not None:
            conditions.append("game_start < ?")
            conditions.append("outcome_finalized_at IS NOT NULL")
            conditions.append("outcome_finalized_at <= ?")
            params.extend((before.isoformat(), before.isoformat()))
        where = " AND ".join(conditions)
        records: list[ProjectionObservationRecord] = []
        offset = 0
        with self._connect() as connection:
            while True:
                rows = connection.execute(
                    """
                    SELECT history_version, player_id, game_id, game_start,
                           outcome_finalized_at, minutes, started, did_not_play,
                           box_score_json, source_version
                    FROM projection_observations
                    WHERE """
                    + where
                    + """
                    ORDER BY game_start, game_id, player_id
                    LIMIT ? OFFSET ?
                    """,
                    (*params, page_size, offset),
                ).fetchall()
                records.extend(self._projection_observation(row) for row in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        return tuple(records)

    @staticmethod
    def _projection_observation(row: tuple[object, ...]) -> ProjectionObservationRecord:
        return ProjectionObservationRecord(
            history_version=str(row[0]),
            player_id=str(row[1]),
            game_id=str(row[2]),
            game_start=datetime.fromisoformat(str(row[3])),
            outcome_finalized_at=datetime.fromisoformat(str(row[4])) if row[4] else None,
            minutes=float(str(row[5])) if row[5] is not None else None,
            started=bool(row[6]),
            did_not_play=bool(row[7]),
            box_score_json=str(row[8]),
            source_version=str(row[9]),
        )

    def upsert_scheduled_work(self, work: ScheduledWorkRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
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
                """,
                self._scheduled_work_values(work),
            )

    def claim_due_work(
        self,
        now: datetime,
        *,
        correlation_id: str,
        lease_duration: timedelta = DEFAULT_SCHEDULED_WORK_LEASE,
        limit: int = 100,
    ) -> tuple[ScheduledWorkRecord, ...]:
        if limit <= 0:
            return ()
        lease_expires_at = now + lease_duration
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
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
                """,
                (
                    lease_expires_at.isoformat(),
                    correlation_id,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    limit,
                ),
            ).fetchall()
        return tuple(self._scheduled_work(row) for row in rows)

    def finish_scheduled_work(
        self,
        work_id: str,
        *,
        status: ScheduledWorkStatus,
        finished_at: datetime,
        correlation_id: str,
        failure_category: str | None = None,
        terminal_summary_json: str | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        if status not in {
            ScheduledWorkStatus.COMPLETED,
            ScheduledWorkStatus.RETRY,
            ScheduledWorkStatus.CANCELED,
        }:
            raise ValueError("Claimed work must finish as completed, retry, or canceled")
        if (status is ScheduledWorkStatus.RETRY) != (retry_at is not None):
            raise ValueError("Retry work requires exactly one retry timestamp")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_work
                SET status = ?, due_at = COALESCE(?, due_at), lease_expires_at = NULL,
                    failure_category = ?, terminal_summary_json = ?, updated_at = ?
                WHERE work_id = ? AND status = 'running' AND correlation_id = ?
                """,
                (
                    status.value,
                    retry_at.isoformat() if retry_at else None,
                    failure_category,
                    terminal_summary_json,
                    finished_at.isoformat(),
                    work_id,
                    correlation_id,
                ),
            )
        return cursor.rowcount == 1

    def list_scheduled_work(
        self,
        *,
        kind: DueWorkKind | None = None,
        statuses: tuple[ScheduledWorkStatus, ...] = (),
    ) -> tuple[ScheduledWorkRecord, ...]:
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
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT work_id, dedupe_key, kind, due_at, status, local_day,
                       game_id, recommendation_id, deadline, lease_expires_at,
                       attempt_count, correlation_id, failure_category,
                       terminal_summary_json, created_at, updated_at
                FROM scheduled_work
                """
                + where
                + " ORDER BY due_at, work_id",
                tuple(params),
            ).fetchall()
        return tuple(self._scheduled_work(row) for row in rows)

    def cancel_expired_scheduled_work(self, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_work
                SET status = 'canceled', lease_expires_at = NULL,
                    failure_category = 'deadline_elapsed', updated_at = ?
                WHERE deadline IS NOT NULL AND deadline <= ? AND (
                    status IN ('pending', 'retry')
                    OR (status = 'running' AND lease_expires_at <= ?)
                )
                """,
                (now.isoformat(), now.isoformat(), now.isoformat()),
            )
        return cursor.rowcount

    @staticmethod
    def _scheduled_work_values(work: ScheduledWorkRecord) -> tuple[object, ...]:
        return (
            work.work_id,
            work.dedupe_key,
            work.kind.value,
            work.due_at.isoformat(),
            work.status.value,
            work.local_day,
            work.game_id,
            work.recommendation_id,
            work.deadline.isoformat() if work.deadline else None,
            work.lease_expires_at.isoformat() if work.lease_expires_at else None,
            work.attempt_count,
            work.correlation_id,
            work.failure_category,
            work.terminal_summary_json,
            work.created_at.isoformat(),
            work.updated_at.isoformat(),
        )

    @staticmethod
    def _scheduled_work(row: tuple[object, ...]) -> ScheduledWorkRecord:
        return ScheduledWorkRecord(
            work_id=str(row[0]),
            dedupe_key=str(row[1]),
            kind=DueWorkKind(str(row[2])),
            due_at=datetime.fromisoformat(str(row[3])),
            status=ScheduledWorkStatus(str(row[4])),
            local_day=str(row[5]) if row[5] is not None else None,
            game_id=str(row[6]) if row[6] is not None else None,
            recommendation_id=str(row[7]) if row[7] is not None else None,
            deadline=datetime.fromisoformat(str(row[8])) if row[8] else None,
            lease_expires_at=datetime.fromisoformat(str(row[9])) if row[9] else None,
            attempt_count=int(str(row[10])),
            correlation_id=str(row[11]) if row[11] is not None else None,
            failure_category=str(row[12]) if row[12] is not None else None,
            terminal_summary_json=str(row[13]) if row[13] is not None else None,
            created_at=datetime.fromisoformat(str(row[14])),
            updated_at=datetime.fromisoformat(str(row[15])),
        )
