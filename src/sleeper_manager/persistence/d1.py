"""Cloudflare D1 executor over shared persistence SQL.

`initialize()` is a no-op; schema is applied by D1 migrations. Token
consumption uses `batch()`. Duplicate action tokens are ignored.
`D1_SCHEMA` is re-exported for tests and FakeD1 bootstrap.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

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
from sleeper_manager.persistence.lock_in_d1 import D1LockInOpportunityMixin
from sleeper_manager.persistence.lock_in_statements import CONSUME_LOCK_IN_OPPORTUNITY_SQL
from sleeper_manager.persistence.rows import (
    acknowledgement_id,
    cached_nba_as_of,
    cached_nba_from_mapping,
    delivery_claim_id,
    freshness_from_mapping,
    freshness_insert_params,
    nba_cache_put_params,
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
    CONSUME_ACKNOWLEDGEMENT_INSERT_SQL,
    CONSUME_LOCK_ACK_SQL,
    CONSUME_RECOMMENDATION_ACK_SQL,
    CONSUME_TOKEN_MARK_USED_SQL,
    D1_SCHEMA,
    DELIVERY_CLAIM_PROVIDER,
    EXPIRE_RECOMMENDATIONS_SQL,
    FINISH_SCHEDULED_WORK_SQL,
    HAS_SUCCESSFUL_DELIVERY_SQL,
    INSERT_DELIVERY_ATTEMPT_SQL,
    INSERT_OR_IGNORE_ACTION_TOKEN_SQL,
    INSERT_RECOMMENDATION_SQL,
    IS_LOCKED_SQL,
    LIST_PENDING_RECOMMENDATIONS_SQL,
    LIST_SCHEDULED_WORK_SQL,
    LOAD_ACTION_TOKEN_ID_SQL,
    LOAD_ACTION_TOKEN_SQL,
    LOAD_DATA_FRESHNESS_SQL,
    LOAD_LEAGUE_SNAPSHOT_SQL,
    LOAD_NBA_CACHE_SQL,
    LOAD_PROJECTION_OBSERVATIONS_SQL,
    LOAD_RECOMMENDATION_SQL,
    LOAD_RUNTIME_POLICY_SQL,
    NEXT_DELIVERY_ATTEMPT_SQL,
    RELEASE_DELIVERY_CLAIM_SQL,
    SUPERSEDE_RECOMMENDATION_SQL,
    UPSERT_DATA_FRESHNESS_SQL,
    UPSERT_LEAGUE_SNAPSHOT_SQL,
    UPSERT_LOCK_ACKNOWLEDGEMENT_SQL,
    UPSERT_NBA_CACHE_SQL,
    UPSERT_PROJECTION_OBSERVATION_SQL,
    UPSERT_RUNTIME_POLICY_SQL,
    UPSERT_SCHEDULED_WORK_SQL,
    projection_observation_filters,
    scheduled_work_filters,
)

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


class D1StateRepository(D1LockInOpportunityMixin, AsyncRuntimeStateRepository):
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
    def _mapping(row: object) -> Mapping[str, Any]:
        if isinstance(row, Mapping):
            return row
        raise AcknowledgementQueryError("unexpected D1 result envelope")

    async def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None:
        await self._run(UPSERT_LEAGUE_SNAPSHOT_SQL, *snapshot_insert_params(snapshot))

    async def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None:
        row = await self._first(LOAD_LEAGUE_SNAPSHOT_SQL, league_id, fantasy_week)
        return snapshot_from_mapping(row) if row is not None else None

    async def save_data_freshness(self, freshness: DataFreshnessRecord) -> None:
        await self._run(UPSERT_DATA_FRESHNESS_SQL, *freshness_insert_params(freshness))

    async def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None:
        row = await self._first(LOAD_DATA_FRESHNESS_SQL, resource)
        return freshness_from_mapping(row) if row is not None else None

    async def create_recommendation(self, recommendation: RecommendationRecord) -> bool:
        result = await self._run(
            INSERT_RECOMMENDATION_SQL,
            *recommendation_insert_params(recommendation),
        )
        return self._changes(result) == 1

    async def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None:
        row = await self._first(LOAD_RECOMMENDATION_SQL, recommendation_id)
        return recommendation_from_mapping(row) if row is not None else None

    async def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None:
        await self._run(
            INSERT_DELIVERY_ATTEMPT_SQL,
            attempt.delivery_id,
            attempt.recommendation_id,
            attempt.provider,
            attempt.attempt_number,
            attempt.attempted_at.isoformat(),
            int(attempt.succeeded),
            attempt.error,
        )

    async def next_delivery_attempt_number(self, recommendation_id: str) -> int:
        row = await self._first(NEXT_DELIVERY_ATTEMPT_SQL, recommendation_id)
        return int(row["attempt_number"]) if row is not None else 1

    async def has_successful_delivery(self, recommendation_id: str) -> bool:
        return await self._first(HAS_SUCCESSFUL_DELIVERY_SQL, recommendation_id) is not None

    async def claim_delivery(
        self,
        recommendation_id: str,
        claimed_at: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> bool:
        result = await self._run(
            CLAIM_DELIVERY_SQL,
            delivery_claim_id(recommendation_id),
            recommendation_id,
            DELIVERY_CLAIM_PROVIDER,
            0,
            claimed_at.isoformat(),
            0,
            None,
            (claimed_at - lease_duration).isoformat(),
        )
        return self._changes(result) == 1

    async def release_delivery_claim(self, recommendation_id: str) -> bool:
        result = await self._run(
            RELEASE_DELIVERY_CLAIM_SQL,
            delivery_claim_id(recommendation_id),
            DELIVERY_CLAIM_PROVIDER,
        )
        return self._changes(result) == 1

    async def create_action_token(self, token: ActionTokenRecord) -> None:
        await self._run(
            INSERT_OR_IGNORE_ACTION_TOKEN_SQL,
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
        token = await self._first(LOAD_ACTION_TOKEN_SQL, token_hash)
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
        token = await self._first(LOAD_ACTION_TOKEN_ID_SQL, token_hash)
        if token is None:
            return AcknowledgementResult(AcknowledgementOutcome.INVALID)
        recommendation_id = str(token["recommendation_id"])
        ack_id = acknowledgement_id(recommendation_id, token_hash)
        statements = [
            self._statement(
                CONSUME_ACKNOWLEDGEMENT_INSERT_SQL,
                (
                    ack_id,
                    action.value,
                    acknowledged_at.isoformat(),
                    token_hash,
                    action.value,
                    acknowledged_at.isoformat(),
                    RecommendationStatus.PENDING.value,
                ),
            ),
            self._statement(
                CONSUME_TOKEN_MARK_USED_SQL,
                (acknowledged_at.isoformat(), token_hash, ack_id),
            ),
            self._statement(
                CONSUME_RECOMMENDATION_ACK_SQL,
                (
                    RecommendationStatus.ACKNOWLEDGED.value,
                    action.value,
                    acknowledged_at.isoformat(),
                    recommendation_id,
                    RecommendationStatus.PENDING.value,
                    ack_id,
                ),
            ),
            self._statement(
                CONSUME_LOCK_ACK_SQL,
                (
                    acknowledged_at.isoformat(),
                    ack_id,
                    AcknowledgementAction.LOCKED.value,
                ),
            ),
            self._statement(
                CONSUME_LOCK_IN_OPPORTUNITY_SQL,
                (
                    action.value,
                    action.value,
                    acknowledged_at.isoformat(),
                    acknowledged_at.isoformat(),
                    recommendation_id,
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
            EXPIRE_RECOMMENDATIONS_SQL,
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
            LIST_PENDING_RECOMMENDATIONS_SQL,
            league_id,
            fantasy_week,
            decision_type,
            RecommendationStatus.PENDING.value,
        )
        records: list[RecommendationRecord] = []
        for row in rows:
            if isinstance(row, Mapping):
                records.append(recommendation_from_mapping(row))
            else:
                raise AcknowledgementQueryError("unexpected D1 result envelope")
        return tuple(records)

    async def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool:
        del now
        result = await self._run(
            SUPERSEDE_RECOMMENDATION_SQL,
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
            UPSERT_LOCK_ACKNOWLEDGEMENT_SQL,
            recommendation_id,
            player_id,
            acknowledged_at.isoformat(),
        )

    async def is_locked(self, recommendation_id: str) -> bool:
        return (
            await self._first(
                IS_LOCKED_SQL,
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
        row = await self._first(LOAD_RUNTIME_POLICY_SQL)
        return runtime_policy_from_mapping(row) if row is not None else None

    async def save_runtime_policy(self, policy: RuntimePolicyRecord) -> None:
        await self._run(
            UPSERT_RUNTIME_POLICY_SQL,
            policy.version,
            policy.payload_json,
            policy.updated_at.isoformat(),
        )

    async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        row = await self._first(LOAD_NBA_CACHE_SQL, cache_key)
        if row is None:
            return None
        return cached_nba_as_of(cached_nba_from_mapping(row), now)

    async def put(self, record: CachedNBARecord) -> None:
        await self._run(UPSERT_NBA_CACHE_SQL, *nba_cache_put_params(record))

    async def save_projection_observations(
        self, observations: tuple[ProjectionObservationRecord, ...]
    ) -> None:
        if not observations:
            return
        statements = [
            self._statement(UPSERT_PROJECTION_OBSERVATION_SQL, projection_observation_params(item))
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
        where, params = projection_observation_filters(history_version, before)
        query = LOAD_PROJECTION_OBSERVATIONS_SQL.format(where=where)
        records: list[ProjectionObservationRecord] = []
        offset = 0
        while True:
            rows = await self._all(query, *params, page_size, offset)
            records.extend(projection_from_mapping(self._mapping(row)) for row in rows)
            if len(rows) < page_size:
                break
            offset += page_size
        return tuple(records)

    async def upsert_scheduled_work(self, work: ScheduledWorkRecord) -> None:
        await self._run(UPSERT_SCHEDULED_WORK_SQL, *scheduled_work_values(work))

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
            CLAIM_DUE_WORK_SQL,
            (now + lease_duration).isoformat(),
            correlation_id,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
            limit,
        )
        return tuple(scheduled_work_from_mapping(self._mapping(row)) for row in rows)

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
        require_terminal_scheduled_status(status, retry_at)
        result = await self._run(
            FINISH_SCHEDULED_WORK_SQL,
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
        where, params = scheduled_work_filters(kind, statuses)
        query = LIST_SCHEDULED_WORK_SQL.format(where=where)
        rows = await self._all(query, *params)
        return tuple(scheduled_work_from_mapping(self._mapping(row)) for row in rows)

    async def cancel_expired_scheduled_work(self, now: datetime) -> int:
        result = await self._run(
            CANCEL_EXPIRED_SCHEDULED_WORK_SQL,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        )
        return self._changes(result)


__all__ = ["D1_SCHEMA", "D1StateRepository"]
