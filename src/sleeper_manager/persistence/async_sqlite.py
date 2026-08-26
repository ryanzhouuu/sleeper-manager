from datetime import datetime, timedelta
from pathlib import Path

from sleeper_manager.domain.planning import AcknowledgedDecisionEvidence
from sleeper_manager.persistence.base import (
    DEFAULT_SCHEDULED_WORK_LEASE,
    PROJECTION_OBSERVATION_PAGE_SIZE,
    AcknowledgementAction,
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
    RuntimePolicyRecord,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.sqlite import SQLiteStateRepository


class AsyncSQLiteStateRepository(AsyncRuntimeStateRepository):
    """Async adapter for local SQLite used by the Worker-shaped test flow."""

    def __init__(self, path: Path) -> None:
        self._repository = SQLiteStateRepository(path)

    async def initialize(self) -> None:
        self._repository.initialize()

    async def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None:
        self._repository.save_league_snapshot(snapshot)

    async def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None:
        return self._repository.load_league_snapshot(league_id, fantasy_week)

    async def save_data_freshness(self, freshness: DataFreshnessRecord) -> None:
        self._repository.save_data_freshness(freshness)

    async def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None:
        return self._repository.load_data_freshness(resource)

    async def create_recommendation(self, recommendation: RecommendationRecord) -> bool:
        return self._repository.create_recommendation(recommendation)

    async def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None:
        return self._repository.get_recommendation(recommendation_id)

    async def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None:
        self._repository.record_delivery_attempt(attempt)

    async def next_delivery_attempt_number(self, recommendation_id: str) -> int:
        return self._repository.next_delivery_attempt_number(recommendation_id)

    async def has_successful_delivery(self, recommendation_id: str) -> bool:
        return self._repository.has_successful_delivery(recommendation_id)

    async def claim_delivery(
        self,
        recommendation_id: str,
        claimed_at: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> bool:
        return self._repository.claim_delivery(
            recommendation_id,
            claimed_at,
            lease_duration=lease_duration,
        )

    async def release_delivery_claim(self, recommendation_id: str) -> bool:
        return self._repository.release_delivery_claim(recommendation_id)

    async def create_action_token(self, token: ActionTokenRecord) -> None:
        self._repository.create_action_token(token)

    async def consume_action_token(
        self,
        token_hash: str,
        action: AcknowledgementAction,
        acknowledged_at: datetime,
    ) -> AcknowledgementResult:
        return self._repository.consume_action_token(token_hash, action, acknowledged_at)

    async def expire_recommendations(self, now: datetime) -> int:
        return self._repository.expire_recommendations(now)

    async def list_pending_recommendations(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        decision_type: str,
    ) -> tuple[RecommendationRecord, ...]:
        return self._repository.list_pending_recommendations(
            league_id,
            fantasy_week,
            decision_type=decision_type,
        )

    async def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool:
        return self._repository.supersede_recommendation(recommendation_id, now)

    async def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None:
        self._repository.record_lock_acknowledgement(
            recommendation_id,
            player_id,
            acknowledged_at,
        )

    async def is_locked(self, recommendation_id: str) -> bool:
        return self._repository.is_locked(recommendation_id)

    async def load_acknowledged_decisions(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        as_of: datetime,
    ) -> tuple[AcknowledgedDecisionEvidence, ...]:
        return self._repository.load_acknowledged_decisions(
            league_id,
            fantasy_week,
            as_of=as_of,
        )

    async def load_runtime_policy(self) -> RuntimePolicyRecord | None:
        return self._repository.load_runtime_policy()

    async def save_runtime_policy(self, policy: RuntimePolicyRecord) -> None:
        self._repository.save_runtime_policy(policy)

    async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        return self._repository.get(cache_key, now=now)

    async def put(self, record: CachedNBARecord) -> None:
        self._repository.put(record)

    async def save_projection_observations(
        self, observations: tuple[ProjectionObservationRecord, ...]
    ) -> None:
        self._repository.save_projection_observations(observations)

    async def load_projection_observations(
        self,
        history_version: str,
        *,
        before: datetime | None = None,
        page_size: int = PROJECTION_OBSERVATION_PAGE_SIZE,
    ) -> tuple[ProjectionObservationRecord, ...]:
        return self._repository.load_projection_observations(
            history_version,
            before=before,
            page_size=page_size,
        )

    async def upsert_scheduled_work(self, work: ScheduledWorkRecord) -> None:
        self._repository.upsert_scheduled_work(work)

    async def claim_due_work(
        self,
        now: datetime,
        *,
        correlation_id: str,
        lease_duration: timedelta = DEFAULT_SCHEDULED_WORK_LEASE,
        limit: int = 100,
    ) -> tuple[ScheduledWorkRecord, ...]:
        return self._repository.claim_due_work(
            now,
            correlation_id=correlation_id,
            lease_duration=lease_duration,
            limit=limit,
        )

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
        return self._repository.finish_scheduled_work(
            work_id,
            status=status,
            finished_at=finished_at,
            correlation_id=correlation_id,
            failure_category=failure_category,
            terminal_summary_json=terminal_summary_json,
            retry_at=retry_at,
        )

    async def list_scheduled_work(
        self,
        *,
        kind: DueWorkKind | None = None,
        statuses: tuple[ScheduledWorkStatus, ...] = (),
    ) -> tuple[ScheduledWorkRecord, ...]:
        return self._repository.list_scheduled_work(kind=kind, statuses=statuses)

    async def cancel_expired_scheduled_work(self, now: datetime) -> int:
        return self._repository.cancel_expired_scheduled_work(now)
