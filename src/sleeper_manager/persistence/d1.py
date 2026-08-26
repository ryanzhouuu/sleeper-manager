import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.planning import AcknowledgedDecisionEvidence
from sleeper_manager.persistence.acknowledgements import (
    ACKNOWLEDGED_DECISIONS_QUERY,
    LOCK_IN_DECISION_TYPE,
    AcknowledgementQueryError,
    decode_acknowledged_decisions,
    raw_row_from_mapping,
    raw_row_from_sequence,
)
from sleeper_manager.persistence.base import (
    DEFAULT_SCHEDULED_WORK_LEASE,
    PROJECTION_OBSERVATION_PAGE_SIZE,
    AcknowledgementAction,
    AcknowledgementOutcome,
    AcknowledgementResult,
    ActionTokenRecord,
    AsyncRuntimeStateRepository,
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

_MISSING = object()


def _js_to_python(value: object) -> object:
    converter = getattr(value, "to_py", None)
    if callable(converter):
        return converter()
    return value


def _d1_field(original: object, payload: object, name: str) -> object:
    if isinstance(payload, Mapping) and name in payload:
        return _js_to_python(payload[name])
    if hasattr(original, name):
        return _js_to_python(getattr(original, name))
    return _MISSING


class D1StateRepository(AsyncRuntimeStateRepository):
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
        if row is None:
            return None
        payload = _js_to_python(row)
        if isinstance(payload, Mapping):
            return {str(key): _js_to_python(value) for key, value in payload.items()}
        raise AcknowledgementQueryError("unexpected D1 result envelope")

    async def _all(self, query: str, *params: object) -> Sequence[object]:
        result = await self._statement(query, params).all()
        payload = _js_to_python(result)
        rows = _d1_field(result, payload, "results")
        if rows is _MISSING or isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
            raise AcknowledgementQueryError("unexpected D1 result envelope")
        self._require_success(result, payload)
        return tuple(_js_to_python(row) for row in rows)

    async def _run(self, query: str, *params: object) -> Any:
        result = await self._statement(query, params).run()
        payload = _js_to_python(result)
        self._require_success(result, payload)
        return result

    @staticmethod
    def _require_success(original: object, payload: object) -> None:
        success = _d1_field(original, payload, "success")
        if success is False:
            raise AcknowledgementQueryError("unsuccessful D1 query")
        if success is not True:
            raise AcknowledgementQueryError("unexpected D1 result envelope")

    @staticmethod
    def _changes(result: Any) -> int:
        payload = _js_to_python(result)
        meta = _d1_field(result, payload, "meta")
        if meta is _MISSING:
            return 0
        meta_payload = _js_to_python(meta)
        changes = _d1_field(meta, meta_payload, "changes")
        if changes is _MISSING:
            return 0
        return int(str(changes))

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
            revision=int(row.get("revision", 0)),
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
                acknowledged_at, trace_json, revision
            ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     COALESCE(MAX(revision), 0) + 1
              FROM recommendations
              WHERE league_id = ? AND fantasy_week = ? AND decision_type = ?
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
            recommendation.league_id,
            recommendation.fantasy_week,
            recommendation.decision_type,
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
            INSERT INTO delivery_attempts (
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

    async def next_delivery_attempt_number(self, recommendation_id: str) -> int:
        row = await self._first(
            """
            SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number
            FROM delivery_attempts
            WHERE recommendation_id = ? AND provider != '_delivery_claim'
            """,
            recommendation_id,
        )
        return int(row["attempt_number"]) if row is not None else 1

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

    @staticmethod
    def _delivery_claim_id(recommendation_id: str) -> str:
        return sha256(f"{recommendation_id}:delivery-claim".encode()).hexdigest()

    async def claim_delivery(
        self,
        recommendation_id: str,
        claimed_at: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> bool:
        result = await self._run(
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
            self._delivery_claim_id(recommendation_id),
            recommendation_id,
            "_delivery_claim",
            0,
            claimed_at.isoformat(),
            0,
            None,
            (claimed_at - lease_duration).isoformat(),
        )
        return self._changes(result) == 1

    async def release_delivery_claim(self, recommendation_id: str) -> bool:
        result = await self._run(
            """
            DELETE FROM delivery_attempts
            WHERE delivery_id = ? AND succeeded = 0 AND provider = ?
            """,
            self._delivery_claim_id(recommendation_id),
            "_delivery_claim",
        )
        return self._changes(result) == 1

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
                WHERE token_hash = ? AND used_at IS NULL AND EXISTS (
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
                INSERT OR IGNORE INTO lock_acknowledgements
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

    async def list_pending_recommendations(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        decision_type: str,
    ) -> tuple[RecommendationRecord, ...]:
        rows = await self._all(
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
            league_id,
            fantasy_week,
            decision_type,
            RecommendationStatus.PENDING.value,
        )
        records: list[RecommendationRecord] = []
        for row in rows:
            if isinstance(row, Mapping):
                records.append(self._recommendation(row))
            else:
                raise AcknowledgementQueryError("unexpected D1 result envelope")
        return tuple(records)

    async def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool:
        del now
        result = await self._run(
            """
            UPDATE recommendations
            SET status = ?
            WHERE recommendation_id = ? AND status = ?
            """,
            RecommendationStatus.SUPERSEDED.value,
            recommendation_id,
            RecommendationStatus.PENDING.value,
        )
        return self._changes(result) == 1

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

    async def load_acknowledged_decisions(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        as_of: datetime,
    ) -> tuple[AcknowledgedDecisionEvidence, ...]:
        rows = await self._all(
            ACKNOWLEDGED_DECISIONS_QUERY,
            league_id,
            fantasy_week,
            LOCK_IN_DECISION_TYPE,
        )
        decoded_rows = []
        for row in rows:
            if isinstance(row, Mapping):
                decoded_rows.append(raw_row_from_mapping(row))
            elif isinstance(row, Sequence) and not isinstance(row, str | bytes):
                decoded_rows.append(raw_row_from_sequence(row))
            else:
                raise AcknowledgementQueryError("unexpected D1 result envelope")
        return decode_acknowledged_decisions(tuple(decoded_rows), as_of=as_of)

    async def load_runtime_policy(self) -> RuntimePolicyRecord | None:
        row = await self._first(
            "SELECT version, payload_json, updated_at FROM runtime_policy WHERE singleton_id = 1"
        )
        if row is None:
            return None
        return RuntimePolicyRecord(
            version=str(row["version"]),
            payload_json=str(row["payload_json"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    async def save_runtime_policy(self, policy: RuntimePolicyRecord) -> None:
        await self._run(
            """
            INSERT INTO runtime_policy (singleton_id, version, payload_json, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                version = excluded.version,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            policy.version,
            policy.payload_json,
            policy.updated_at.isoformat(),
        )

    async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        row = await self._first(
            """
            SELECT cache_key, provider, resource, schema_version, payload_json,
                   retrieved_at, source_updated_at, expires_at, quality,
                   warnings_json, errors_json
            FROM nba_cache WHERE cache_key = ?
            """,
            cache_key,
        )
        if row is None:
            return None
        record = CachedNBARecord(
            cache_key=str(row["cache_key"]),
            provider=str(row["provider"]),
            resource=str(row["resource"]),
            schema_version=str(row["schema_version"]),
            payload_json=str(row["payload_json"]),
            retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            source_updated_at=(
                datetime.fromisoformat(str(row["source_updated_at"]))
                if row.get("source_updated_at")
                else None
            ),
            expires_at=(
                datetime.fromisoformat(str(row["expires_at"])) if row.get("expires_at") else None
            ),
            quality=DataQualityState(str(row["quality"])),
            warnings=tuple(json.loads(str(row["warnings_json"]))),
            errors=tuple(json.loads(str(row["errors_json"]))),
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

    async def put(self, record: CachedNBARecord) -> None:
        await self._run(
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
        )

    async def save_projection_observations(
        self, observations: tuple[ProjectionObservationRecord, ...]
    ) -> None:
        if not observations:
            return
        query = """
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
        statements = [
            self._statement(
                query,
                (
                    item.history_version,
                    item.player_id,
                    item.game_id,
                    item.game_start.isoformat(),
                    item.outcome_finalized_at.isoformat() if item.outcome_finalized_at else None,
                    item.minutes,
                    int(item.started),
                    int(item.did_not_play),
                    item.box_score_json,
                    item.source_version,
                ),
            )
            for item in observations
        ]
        results = await self._database.batch(statements)
        for result in results:
            payload = _js_to_python(result)
            self._require_success(result, payload)

    async def load_projection_observations(
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
        while True:
            rows = await self._all(
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
                *params,
                page_size,
                offset,
            )
            records.extend(self._projection_observation(self._mapping(row)) for row in rows)
            if len(rows) < page_size:
                break
            offset += page_size
        return tuple(records)

    async def upsert_scheduled_work(self, work: ScheduledWorkRecord) -> None:
        await self._run(
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
            *self._scheduled_work_values(work),
        )

    async def claim_due_work(
        self,
        now: datetime,
        *,
        correlation_id: str,
        lease_duration: timedelta = DEFAULT_SCHEDULED_WORK_LEASE,
        limit: int = 100,
    ) -> tuple[ScheduledWorkRecord, ...]:
        if limit <= 0:
            return ()
        rows = await self._all(
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
            (now + lease_duration).isoformat(),
            correlation_id,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
            limit,
        )
        return tuple(self._scheduled_work(self._mapping(row)) for row in rows)

    async def finish_scheduled_work(
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
        result = await self._run(
            """
            UPDATE scheduled_work
            SET status = ?, due_at = COALESCE(?, due_at), lease_expires_at = NULL,
                failure_category = ?, terminal_summary_json = ?, updated_at = ?
            WHERE work_id = ? AND status = 'running' AND correlation_id = ?
            """,
            status.value,
            retry_at.isoformat() if retry_at else None,
            failure_category,
            terminal_summary_json,
            finished_at.isoformat(),
            work_id,
            correlation_id,
        )
        return self._changes(result) == 1

    async def list_scheduled_work(
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
        rows = await self._all(
            """
            SELECT work_id, dedupe_key, kind, due_at, status, local_day,
                   game_id, recommendation_id, deadline, lease_expires_at,
                   attempt_count, correlation_id, failure_category,
                   terminal_summary_json, created_at, updated_at
            FROM scheduled_work
            """
            + where
            + " ORDER BY due_at, work_id",
            *params,
        )
        return tuple(self._scheduled_work(self._mapping(row)) for row in rows)

    async def cancel_expired_scheduled_work(self, now: datetime) -> int:
        result = await self._run(
            """
            UPDATE scheduled_work
            SET status = 'canceled', lease_expires_at = NULL,
                failure_category = 'deadline_elapsed', updated_at = ?
            WHERE deadline IS NOT NULL AND deadline <= ? AND (
                status IN ('pending', 'retry')
                OR (status = 'running' AND lease_expires_at <= ?)
            )
            """,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        )
        return self._changes(result)

    @staticmethod
    def _mapping(row: object) -> Mapping[str, Any]:
        if isinstance(row, Mapping):
            return row
        raise AcknowledgementQueryError("unexpected D1 result envelope")

    @staticmethod
    def _projection_observation(row: Mapping[str, Any]) -> ProjectionObservationRecord:
        return ProjectionObservationRecord(
            history_version=str(row["history_version"]),
            player_id=str(row["player_id"]),
            game_id=str(row["game_id"]),
            game_start=datetime.fromisoformat(str(row["game_start"])),
            outcome_finalized_at=(
                datetime.fromisoformat(str(row["outcome_finalized_at"]))
                if row.get("outcome_finalized_at")
                else None
            ),
            minutes=float(row["minutes"]) if row.get("minutes") is not None else None,
            started=bool(row["started"]),
            did_not_play=bool(row["did_not_play"]),
            box_score_json=str(row["box_score_json"]),
            source_version=str(row["source_version"]),
        )

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
    def _scheduled_work(row: Mapping[str, Any]) -> ScheduledWorkRecord:
        return ScheduledWorkRecord(
            work_id=str(row["work_id"]),
            dedupe_key=str(row["dedupe_key"]),
            kind=DueWorkKind(str(row["kind"])),
            due_at=datetime.fromisoformat(str(row["due_at"])),
            status=ScheduledWorkStatus(str(row["status"])),
            local_day=str(row["local_day"]) if row.get("local_day") is not None else None,
            game_id=str(row["game_id"]) if row.get("game_id") is not None else None,
            recommendation_id=(
                str(row["recommendation_id"]) if row.get("recommendation_id") is not None else None
            ),
            deadline=(
                datetime.fromisoformat(str(row["deadline"])) if row.get("deadline") else None
            ),
            lease_expires_at=(
                datetime.fromisoformat(str(row["lease_expires_at"]))
                if row.get("lease_expires_at")
                else None
            ),
            attempt_count=int(row["attempt_count"]),
            correlation_id=(
                str(row["correlation_id"]) if row.get("correlation_id") is not None else None
            ),
            failure_category=(
                str(row["failure_category"]) if row.get("failure_category") is not None else None
            ),
            terminal_summary_json=(
                str(row["terminal_summary_json"])
                if row.get("terminal_summary_json") is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
