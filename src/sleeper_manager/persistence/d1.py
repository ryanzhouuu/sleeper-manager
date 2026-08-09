import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    AcknowledgementResult,
    ActionTokenRecord,
    AsyncStateRepository,
    DataFreshnessRecord,
    DeliveryAttemptRecord,
    LeagueSnapshotRecord,
    RecommendationRecord,
    RecommendationStatus,
)

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
    trace_json TEXT NOT NULL
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
"""


class D1StateRepository(AsyncStateRepository):
    """Async repository backed by a Cloudflare D1 binding."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def initialize(self) -> None:
        return None

    def _statement(self, query: str, params: Sequence[object] = ()) -> Any:
        statement = self._database.prepare(query)
        return statement.bind(*params) if params else statement

    async def _first(self, query: str, *params: object) -> dict[str, Any] | None:
        row = await self._statement(query, params).first()
        return dict(row) if isinstance(row, Mapping) else None

    async def _run(self, query: str, *params: object) -> Any:
        return await self._statement(query, params).run()

    @staticmethod
    def _changes(result: Any) -> int:
        if isinstance(result, Mapping):
            meta = result.get("meta")
            if isinstance(meta, Mapping):
                return int(meta.get("changes", 0))
        return 0

    @staticmethod
    def _recommendation(row: Mapping[str, Any]) -> RecommendationRecord:
        return RecommendationRecord(
            recommendation_id=str(row["recommendation_id"]),
            idempotency_key=str(row["idempotency_key"]),
            league_id=str(row["league_id"]),
            fantasy_week=int(row["fantasy_week"]),
            player_id=str(row["player_id"]),
            game_id=str(row["game_id"]) if row.get("game_id") is not None else None,
            decision_type=str(row["decision_type"]),
            title=str(row["title"]),
            message=str(row["message"]),
            deadline=(
                datetime.fromisoformat(str(row["deadline"])) if row.get("deadline") else None
            ),
            policy_version=str(row["policy_version"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            status=RecommendationStatus(str(row["status"])),
            acknowledged_action=(
                AcknowledgementAction(str(row["acknowledged_action"]))
                if row.get("acknowledged_action")
                else None
            ),
            acknowledged_at=(
                datetime.fromisoformat(str(row["acknowledged_at"]))
                if row.get("acknowledged_at")
                else None
            ),
            trace_json=str(row.get("trace_json", "{}")),
        )

    async def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None:
        await self._run(
            """
            INSERT INTO league_snapshots (
                snapshot_id, league_id, fantasy_week, payload_json, retrieved_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(league_id, fantasy_week) DO UPDATE SET
                snapshot_id = excluded.snapshot_id,
                payload_json = excluded.payload_json,
                retrieved_at = excluded.retrieved_at
            """,
            snapshot.snapshot_id,
            snapshot.league_id,
            snapshot.fantasy_week,
            snapshot.payload_json,
            snapshot.retrieved_at.isoformat(),
        )

    async def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None:
        row = await self._first(
            """
            SELECT snapshot_id, league_id, fantasy_week, payload_json, retrieved_at
            FROM league_snapshots WHERE league_id = ? AND fantasy_week = ?
            """,
            league_id,
            fantasy_week,
        )
        if row is None:
            return None
        return LeagueSnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            league_id=str(row["league_id"]),
            fantasy_week=int(row["fantasy_week"]),
            payload_json=str(row["payload_json"]),
            retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
        )

    async def save_data_freshness(self, freshness: DataFreshnessRecord) -> None:
        await self._run(
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
            freshness.resource,
            freshness.retrieved_at.isoformat(),
            freshness.expires_at.isoformat() if freshness.expires_at else None,
            freshness.quality.value,
            json.dumps(freshness.warnings),
            json.dumps(freshness.errors),
        )

    async def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None:
        row = await self._first(
            """
            SELECT resource, retrieved_at, expires_at, quality, warnings_json, errors_json
            FROM data_freshness WHERE resource = ?
            """,
            resource,
        )
        if row is None:
            return None
        return DataFreshnessRecord(
            resource=str(row["resource"]),
            retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            expires_at=(
                datetime.fromisoformat(str(row["expires_at"])) if row.get("expires_at") else None
            ),
            quality=DataQualityState(str(row["quality"])),
            warnings=tuple(json.loads(str(row["warnings_json"]))),
            errors=tuple(json.loads(str(row["errors_json"]))),
        )

    async def create_recommendation(self, recommendation: RecommendationRecord) -> bool:
        result = await self._run(
            """
            INSERT OR IGNORE INTO recommendations (
                recommendation_id, idempotency_key, league_id, fantasy_week,
                player_id, game_id, decision_type, title, message, deadline,
                policy_version, created_at, status, acknowledged_action,
                acknowledged_at, trace_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            recommendation.recommendation_id,
            recommendation.idempotency_key,
            recommendation.league_id,
            recommendation.fantasy_week,
            recommendation.player_id,
            recommendation.game_id,
            recommendation.decision_type,
            recommendation.title,
            recommendation.message,
            recommendation.deadline.isoformat() if recommendation.deadline else None,
            recommendation.policy_version,
            recommendation.created_at.isoformat(),
            recommendation.status.value,
            recommendation.acknowledged_action.value
            if recommendation.acknowledged_action
            else None,
            recommendation.acknowledged_at.isoformat() if recommendation.acknowledged_at else None,
            recommendation.trace_json,
        )
        return self._changes(result) == 1

    async def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None:
        row = await self._first(
            "SELECT * FROM recommendations WHERE recommendation_id = ?",
            recommendation_id,
        )
        return self._recommendation(row) if row is not None else None

    async def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None:
        await self._run(
            """
            INSERT OR REPLACE INTO delivery_attempts (
                delivery_id, recommendation_id, provider, attempt_number,
                attempted_at, succeeded, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            attempt.delivery_id,
            attempt.recommendation_id,
            attempt.provider,
            attempt.attempt_number,
            attempt.attempted_at.isoformat(),
            int(attempt.succeeded),
            attempt.error,
        )

    async def has_successful_delivery(self, recommendation_id: str) -> bool:
        return (
            await self._first(
                """
                SELECT 1 AS found FROM delivery_attempts
                WHERE recommendation_id = ? AND succeeded = 1 LIMIT 1
                """,
                recommendation_id,
            )
            is not None
        )

    async def create_action_token(self, token: ActionTokenRecord) -> None:
        await self._run(
            """
            INSERT OR IGNORE INTO action_tokens (
                token_hash, recommendation_id, action, created_at, expires_at, used_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            token.token_hash,
            token.recommendation_id,
            token.action.value,
            token.created_at.isoformat(),
            token.expires_at.isoformat(),
            token.used_at.isoformat() if token.used_at else None,
        )

    async def _acknowledgement_outcome(
        self,
        token_hash: str,
        action: AcknowledgementAction,
        acknowledged_at: datetime,
    ) -> AcknowledgementResult:
        token = await self._first(
            """
            SELECT recommendation_id, action, expires_at, used_at
            FROM action_tokens WHERE token_hash = ?
            """,
            token_hash,
        )
        if token is None:
            return AcknowledgementResult(AcknowledgementOutcome.INVALID)
        recommendation = await self.get_recommendation(str(token["recommendation_id"]))
        if recommendation is None:
            return AcknowledgementResult(AcknowledgementOutcome.INVALID)
        if (
            token.get("used_at") is not None
            or recommendation.status is RecommendationStatus.ACKNOWLEDGED
        ):
            return AcknowledgementResult(AcknowledgementOutcome.ALREADY_USED, recommendation)
        if datetime.fromisoformat(str(token["expires_at"])) <= acknowledged_at:
            return AcknowledgementResult(AcknowledgementOutcome.EXPIRED, recommendation)
        if str(token["action"]) != action.value:
            return AcknowledgementResult(AcknowledgementOutcome.CONFLICT, recommendation)
        if recommendation.status is not RecommendationStatus.PENDING:
            return AcknowledgementResult(AcknowledgementOutcome.CONFLICT, recommendation)
        return AcknowledgementResult(AcknowledgementOutcome.CONFLICT, recommendation)

    async def consume_action_token(
        self,
        token_hash: str,
        action: AcknowledgementAction,
        acknowledged_at: datetime,
    ) -> AcknowledgementResult:
        token = await self._first(
            "SELECT recommendation_id FROM action_tokens WHERE token_hash = ?",
            token_hash,
        )
        if token is None:
            return AcknowledgementResult(AcknowledgementOutcome.INVALID)
        recommendation_id = str(token["recommendation_id"])
        acknowledgement_id = sha256(f"{recommendation_id}:{token_hash}".encode()).hexdigest()
        statements = [
            self._statement(
                """
                INSERT OR IGNORE INTO acknowledgements (
                    acknowledgement_id, recommendation_id, action, acknowledged_at, token_hash
                )
                SELECT ?, r.recommendation_id, ?, ?, t.token_hash
                FROM action_tokens t
                JOIN recommendations r ON r.recommendation_id = t.recommendation_id
                WHERE t.token_hash = ? AND t.action = ? AND t.used_at IS NULL
                  AND t.expires_at > ? AND r.status = ?
                """,
                (
                    acknowledgement_id,
                    action.value,
                    acknowledged_at.isoformat(),
                    token_hash,
                    action.value,
                    acknowledged_at.isoformat(),
                    RecommendationStatus.PENDING.value,
                ),
            ),
            self._statement(
                """
                UPDATE action_tokens SET used_at = ?
                WHERE token_hash = ? AND EXISTS (
                    SELECT 1 FROM acknowledgements WHERE acknowledgement_id = ?
                )
                """,
                (acknowledged_at.isoformat(), token_hash, acknowledgement_id),
            ),
            self._statement(
                """
                UPDATE recommendations
                SET status = ?, acknowledged_action = ?, acknowledged_at = ?
                WHERE recommendation_id = ? AND status = ? AND EXISTS (
                    SELECT 1 FROM acknowledgements WHERE acknowledgement_id = ?
                )
                """,
                (
                    RecommendationStatus.ACKNOWLEDGED.value,
                    action.value,
                    acknowledged_at.isoformat(),
                    recommendation_id,
                    RecommendationStatus.PENDING.value,
                    acknowledgement_id,
                ),
            ),
            self._statement(
                """
                INSERT OR REPLACE INTO lock_acknowledgements
                    (recommendation_id, player_id, acknowledged_at)
                SELECT r.recommendation_id, r.player_id, ?
                FROM recommendations r
                JOIN acknowledgements a ON a.recommendation_id = r.recommendation_id
                WHERE a.acknowledgement_id = ? AND a.action = ?
                """,
                (
                    acknowledged_at.isoformat(),
                    acknowledgement_id,
                    AcknowledgementAction.LOCKED.value,
                ),
            ),
        ]
        results = await self._database.batch(statements)
        if isinstance(results, list) and results and self._changes(results[0]) == 1:
            return AcknowledgementResult(
                AcknowledgementOutcome.APPLIED,
                await self.get_recommendation(recommendation_id),
            )
        return await self._acknowledgement_outcome(token_hash, action, acknowledged_at)

    async def expire_recommendations(self, now: datetime) -> int:
        result = await self._run(
            """
            UPDATE recommendations SET status = ?
            WHERE status = ? AND deadline IS NOT NULL AND deadline <= ?
            """,
            RecommendationStatus.EXPIRED.value,
            RecommendationStatus.PENDING.value,
            now.isoformat(),
        )
        return self._changes(result)

    async def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None:
        await self._run(
            """
            INSERT OR REPLACE INTO lock_acknowledgements
                (recommendation_id, player_id, acknowledged_at)
            VALUES (?, ?, ?)
            """,
            recommendation_id,
            player_id,
            acknowledged_at.isoformat(),
        )

    async def is_locked(self, recommendation_id: str) -> bool:
        return (
            await self._first(
                """
                SELECT 1 AS found FROM lock_acknowledgements WHERE recommendation_id = ?
                UNION ALL
                SELECT 1 AS found FROM recommendations
                WHERE recommendation_id = ? AND status = ? AND acknowledged_action = ?
                LIMIT 1
                """,
                recommendation_id,
                recommendation_id,
                RecommendationStatus.ACKNOWLEDGED.value,
                AcknowledgementAction.LOCKED.value,
            )
            is not None
        )
