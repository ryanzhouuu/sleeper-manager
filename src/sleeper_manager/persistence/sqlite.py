import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
from sleeper_manager.persistence.rows import (
    acknowledgement_id,
    cached_nba_as_of,
    cached_nba_from_mapping,
    delivery_claim_id,
    freshness_from_mapping,
    freshness_insert_params,
    nba_cache_put_params,
    profile_from_mapping,
    projection_from_mapping,
    projection_observation_params,
    recommendation_from_mapping,
    recommendation_insert_params,
    require_terminal_scheduled_status,
    runtime_policy_from_mapping,
    scheduled_work_from_mapping,
    scheduled_work_values,
    snapshot_from_mapping,
    snapshot_insert_params,
)
from sleeper_manager.persistence.statements import (
    CANCEL_EXPIRED_SCHEDULED_WORK_SQL,
    CLAIM_DELIVERY_SQL,
    CLAIM_DUE_WORK_SQL,
    DELIVERY_CLAIM_PROVIDER,
    EXPIRE_RECOMMENDATIONS_SQL,
    FINISH_SCHEDULED_WORK_SQL,
    HAS_SUCCESSFUL_DELIVERY_SQL,
    INSERT_ACTION_TOKEN_SQL,
    INSERT_DELIVERY_ATTEMPT_SQL,
    INSERT_RECOMMENDATION_SQL,
    IS_LOCKED_SQL,
    LIST_PENDING_RECOMMENDATIONS_SQL,
    LIST_SCHEDULED_WORK_SQL,
    LOAD_ACTION_TOKEN_SQL,
    LOAD_DATA_FRESHNESS_SQL,
    LOAD_LEAGUE_PROFILE_SQL,
    LOAD_LEAGUE_SNAPSHOT_SQL,
    LOAD_NBA_CACHE_SQL,
    LOAD_PROJECTION_OBSERVATIONS_SQL,
    LOAD_RECOMMENDATION_SQL,
    LOAD_RUNTIME_POLICY_SQL,
    NEXT_DELIVERY_ATTEMPT_SQL,
    RELEASE_DELIVERY_CLAIM_SQL,
    SQLITE_CORE_SCHEMA,
    SQLITE_REVISION_BACKFILL_SQL,
    SQLITE_RUNTIME_SCHEMA,
    SUPERSEDE_RECOMMENDATION_SQL,
    UPSERT_DATA_FRESHNESS_SQL,
    UPSERT_LEAGUE_PROFILE_SQL,
    UPSERT_LEAGUE_SNAPSHOT_SQL,
    UPSERT_LOCK_ACKNOWLEDGEMENT_SQL,
    UPSERT_NBA_CACHE_SQL,
    UPSERT_PROJECTION_OBSERVATION_SQL,
    UPSERT_RUNTIME_POLICY_SQL,
    UPSERT_SCHEDULED_WORK_SQL,
    projection_observation_filters,
    scheduled_work_filters,
)


def _mapping(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


class SQLiteStateRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SQLITE_CORE_SCHEMA)
            connection.execute(ACKNOWLEDGED_DECISIONS_INDEX_SQL)
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(recommendations)")
            }
            if "revision" not in columns:
                connection.execute(
                    "ALTER TABLE recommendations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(SQLITE_REVISION_BACKFILL_SQL)
            connection.executescript(SQLITE_RUNTIME_SCHEMA)

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

    def load_profile(self, league_id: str) -> StoredLeagueProfile | None:
        with self._connect() as connection:
            row = _mapping(connection.execute(LOAD_LEAGUE_PROFILE_SQL, (league_id,)).fetchone())
        return profile_from_mapping(row) if row is not None else None

    def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None:
        with self._connect() as connection:
            connection.execute(UPSERT_LEAGUE_SNAPSHOT_SQL, snapshot_insert_params(snapshot))

    def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None:
        with self._connect() as connection:
            row = _mapping(
                connection.execute(LOAD_LEAGUE_SNAPSHOT_SQL, (league_id, fantasy_week)).fetchone()
            )
        return snapshot_from_mapping(row) if row is not None else None

    def save_data_freshness(self, freshness: DataFreshnessRecord) -> None:
        with self._connect() as connection:
            connection.execute(UPSERT_DATA_FRESHNESS_SQL, freshness_insert_params(freshness))

    def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None:
        with self._connect() as connection:
            row = _mapping(connection.execute(LOAD_DATA_FRESHNESS_SQL, (resource,)).fetchone())
        return freshness_from_mapping(row) if row is not None else None

    def save_profile(self, profile: StoredLeagueProfile) -> None:
        with self._connect() as connection:
            connection.execute(
                UPSERT_LEAGUE_PROFILE_SQL,
                (profile.league_id, profile.fingerprint, profile.retrieved_at.isoformat()),
            )

    def create_recommendation(self, recommendation: RecommendationRecord) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                INSERT_RECOMMENDATION_SQL, recommendation_insert_params(recommendation)
            )
        return cursor.rowcount == 1

    def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None:
        with self._connect() as connection:
            row = _mapping(
                connection.execute(LOAD_RECOMMENDATION_SQL, (recommendation_id,)).fetchone()
            )
        return recommendation_from_mapping(row) if row is not None else None

    def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                INSERT_DELIVERY_ATTEMPT_SQL,
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
            row = _mapping(
                connection.execute(NEXT_DELIVERY_ATTEMPT_SQL, (recommendation_id,)).fetchone()
            )
        if row is None:
            return 1
        return int(row["attempt_number"])

    def has_successful_delivery(self, recommendation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(HAS_SUCCESSFUL_DELIVERY_SQL, (recommendation_id,)).fetchone()
        return row is not None

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
                CLAIM_DELIVERY_SQL,
                (
                    delivery_claim_id(recommendation_id),
                    recommendation_id,
                    DELIVERY_CLAIM_PROVIDER,
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
                RELEASE_DELIVERY_CLAIM_SQL,
                (delivery_claim_id(recommendation_id), DELIVERY_CLAIM_PROVIDER),
            )
        return cursor.rowcount == 1

    def create_action_token(self, token: ActionTokenRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                INSERT_ACTION_TOKEN_SQL,
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
            token = _mapping(connection.execute(LOAD_ACTION_TOKEN_SQL, (token_hash,)).fetchone())
            if token is None:
                return AcknowledgementResult(AcknowledgementOutcome.INVALID)
            if token["used_at"] is not None:
                return AcknowledgementResult(AcknowledgementOutcome.ALREADY_USED)
            if datetime.fromisoformat(str(token["expires_at"])) <= acknowledged_at:
                return AcknowledgementResult(AcknowledgementOutcome.EXPIRED)
            if str(token["action"]) != action.value:
                return AcknowledgementResult(AcknowledgementOutcome.CONFLICT)

            recommendation_row = _mapping(
                connection.execute(
                    LOAD_RECOMMENDATION_SQL, (token["recommendation_id"],)
                ).fetchone()
            )
            if recommendation_row is None:
                return AcknowledgementResult(AcknowledgementOutcome.INVALID)
            recommendation = recommendation_from_mapping(recommendation_row)
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
            connection.execute(
                """
                INSERT INTO acknowledgements (
                    acknowledgement_id, recommendation_id, action, acknowledged_at, token_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    acknowledgement_id(recommendation.recommendation_id, token_hash),
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
            updated = _mapping(
                connection.execute(
                    LOAD_RECOMMENDATION_SQL, (recommendation.recommendation_id,)
                ).fetchone()
            )
        return AcknowledgementResult(
            AcknowledgementOutcome.APPLIED,
            recommendation_from_mapping(updated) if updated is not None else None,
        )

    def expire_recommendations(self, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                EXPIRE_RECOMMENDATIONS_SQL,
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
                LIST_PENDING_RECOMMENDATIONS_SQL,
                (
                    league_id,
                    fantasy_week,
                    decision_type,
                    RecommendationStatus.PENDING.value,
                ),
            ).fetchall()
        return tuple(
            recommendation_from_mapping(mapped)
            for row in rows
            if (mapped := _mapping(row)) is not None
        )

    def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool:
        del now
        with self._connect() as connection:
            cursor = connection.execute(
                SUPERSEDE_RECOMMENDATION_SQL,
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
                UPSERT_LOCK_ACKNOWLEDGEMENT_SQL,
                (recommendation_id, player_id, acknowledged_at.isoformat()),
            )

    def is_locked(self, recommendation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                IS_LOCKED_SQL,
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
            row = _mapping(connection.execute(LOAD_RUNTIME_POLICY_SQL).fetchone())
        return runtime_policy_from_mapping(row) if row is not None else None

    def save_runtime_policy(self, policy: RuntimePolicyRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                UPSERT_RUNTIME_POLICY_SQL,
                (policy.version, policy.payload_json, policy.updated_at.isoformat()),
            )

    def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        with self._connect() as connection:
            row = _mapping(connection.execute(LOAD_NBA_CACHE_SQL, (cache_key,)).fetchone())
        if row is None:
            return None
        return cached_nba_as_of(cached_nba_from_mapping(row), now)

    def put(self, record: CachedNBARecord) -> None:
        with self._connect() as connection:
            connection.execute(UPSERT_NBA_CACHE_SQL, nba_cache_put_params(record))

    def save_projection_observations(
        self, observations: tuple[ProjectionObservationRecord, ...]
    ) -> None:
        with self._connect() as connection:
            connection.executemany(
                UPSERT_PROJECTION_OBSERVATION_SQL,
                tuple(projection_observation_params(item) for item in observations),
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
        where, params = projection_observation_filters(history_version, before)
        query = LOAD_PROJECTION_OBSERVATIONS_SQL.format(where=where)
        records: list[ProjectionObservationRecord] = []
        offset = 0
        with self._connect() as connection:
            while True:
                rows = connection.execute(query, (*params, page_size, offset)).fetchall()
                records.extend(
                    projection_from_mapping(mapped)
                    for row in rows
                    if (mapped := _mapping(row)) is not None
                )
                if len(rows) < page_size:
                    break
                offset += page_size
        return tuple(records)

    def upsert_scheduled_work(self, work: ScheduledWorkRecord) -> None:
        with self._connect() as connection:
            connection.execute(UPSERT_SCHEDULED_WORK_SQL, scheduled_work_values(work))

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
                CLAIM_DUE_WORK_SQL,
                (
                    lease_expires_at.isoformat(),
                    correlation_id,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    limit,
                ),
            ).fetchall()
        return tuple(
            scheduled_work_from_mapping(mapped)
            for row in rows
            if (mapped := _mapping(row)) is not None
        )

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
        require_terminal_scheduled_status(status, retry_at)
        with self._connect() as connection:
            cursor = connection.execute(
                FINISH_SCHEDULED_WORK_SQL,
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
        where, params = scheduled_work_filters(kind, statuses)
        query = LIST_SCHEDULED_WORK_SQL.format(where=where)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(
            scheduled_work_from_mapping(mapped)
            for row in rows
            if (mapped := _mapping(row)) is not None
        )

    def cancel_expired_scheduled_work(self, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                CANCEL_EXPIRED_SCHEDULED_WORK_SQL,
                (now.isoformat(), now.isoformat(), now.isoformat()),
            )
        return cursor.rowcount
