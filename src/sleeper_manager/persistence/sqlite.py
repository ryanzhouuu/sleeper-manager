import json
import sqlite3
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    AcknowledgementResult,
    ActionTokenRecord,
    DataFreshnessRecord,
    DeliveryAttemptRecord,
    LeagueSnapshotRecord,
    RecommendationRecord,
    RecommendationStatus,
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
                    trace_json TEXT NOT NULL
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
                    acknowledged_at, trace_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                       acknowledged_at, trace_json
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
                INSERT OR REPLACE INTO delivery_attempts (
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
                       acknowledged_at, trace_json
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
                "UPDATE action_tokens SET used_at = ? WHERE token_hash = ?",
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
                    INSERT OR REPLACE INTO lock_acknowledgements
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
                       acknowledged_at, trace_json
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
