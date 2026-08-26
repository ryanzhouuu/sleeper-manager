from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.planning import AcknowledgedDecisionEvidence

DEFAULT_SCHEDULED_WORK_LEASE = timedelta(minutes=15)
PROJECTION_OBSERVATION_PAGE_SIZE = 1000


@dataclass(frozen=True, slots=True)
class StoredLeagueProfile:
    league_id: str
    fingerprint: str
    retrieved_at: datetime


class LeagueProfileStore(Protocol):
    def load_profile(self, league_id: str) -> StoredLeagueProfile | None: ...

    def save_profile(self, profile: StoredLeagueProfile) -> None: ...


@dataclass(frozen=True, slots=True)
class CachedNBARecord:
    cache_key: str
    provider: str
    resource: str
    schema_version: str
    payload_json: str
    retrieved_at: datetime
    source_updated_at: datetime | None
    expires_at: datetime | None
    quality: DataQualityState
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class NBADataCache(Protocol):
    def initialize(self) -> None: ...

    def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None: ...

    def put(self, record: CachedNBARecord) -> None: ...


class AsyncNBADataCache(Protocol):
    async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None: ...

    async def put(self, record: CachedNBARecord) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimePolicyRecord:
    version: str
    payload_json: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectionObservationRecord:
    history_version: str
    player_id: str
    game_id: str
    game_start: datetime
    outcome_finalized_at: datetime | None
    minutes: float | None
    started: bool
    did_not_play: bool
    box_score_json: str
    source_version: str


class DueWorkKind(StrEnum):
    DAILY = "daily"
    PRE_TIPOFF = "pre_tipoff"
    DELIVERY_RETRY = "delivery_retry"


class ScheduledWorkStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    COMPLETED = "completed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class ScheduledWorkRecord:
    work_id: str
    dedupe_key: str
    kind: DueWorkKind
    due_at: datetime
    status: ScheduledWorkStatus
    created_at: datetime
    updated_at: datetime
    local_day: str | None = None
    game_id: str | None = None
    recommendation_id: str | None = None
    deadline: datetime | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    correlation_id: str | None = None
    failure_category: str | None = None
    terminal_summary_json: str | None = None


@dataclass(frozen=True, slots=True)
class LeagueSnapshotRecord:
    snapshot_id: str
    league_id: str
    fantasy_week: int
    payload_json: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class DataFreshnessRecord:
    resource: str
    retrieved_at: datetime
    expires_at: datetime | None
    quality: DataQualityState
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class RecommendationStatus(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AcknowledgementAction(StrEnum):
    LOCKED = "locked"
    PASSED = "passed"


class AcknowledgementOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_USED = "already_used"
    EXPIRED = "expired"
    INVALID = "invalid"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    recommendation_id: str
    idempotency_key: str
    league_id: str
    fantasy_week: int
    player_id: str
    game_id: str | None
    decision_type: str
    title: str
    message: str
    deadline: datetime | None
    policy_version: str
    created_at: datetime
    status: RecommendationStatus = RecommendationStatus.PENDING
    acknowledged_action: AcknowledgementAction | None = None
    acknowledged_at: datetime | None = None
    trace_json: str = "{}"
    revision: int = 0


@dataclass(frozen=True, slots=True)
class DeliveryAttemptRecord:
    delivery_id: str
    recommendation_id: str
    provider: str
    attempt_number: int
    attempted_at: datetime
    succeeded: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ActionTokenRecord:
    token_hash: str
    recommendation_id: str
    action: AcknowledgementAction
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AcknowledgementRecord:
    acknowledgement_id: str
    recommendation_id: str
    action: AcknowledgementAction
    acknowledged_at: datetime
    token_hash: str


@dataclass(frozen=True, slots=True)
class AcknowledgementResult:
    outcome: AcknowledgementOutcome
    recommendation: RecommendationRecord | None = None


class StateRepository(Protocol):
    def initialize(self) -> None: ...

    def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None: ...

    def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None: ...

    def save_data_freshness(self, freshness: DataFreshnessRecord) -> None: ...

    def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None: ...

    def create_recommendation(self, recommendation: RecommendationRecord) -> bool: ...

    def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None: ...

    def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None: ...

    def next_delivery_attempt_number(self, recommendation_id: str) -> int: ...

    def has_successful_delivery(self, recommendation_id: str) -> bool: ...

    def claim_delivery(
        self,
        recommendation_id: str,
        claimed_at: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> bool: ...

    def release_delivery_claim(self, recommendation_id: str) -> bool: ...

    def create_action_token(self, token: ActionTokenRecord) -> None: ...

    def consume_action_token(
        self,
        token_hash: str,
        action: AcknowledgementAction,
        acknowledged_at: datetime,
    ) -> AcknowledgementResult: ...

    def expire_recommendations(self, now: datetime) -> int: ...

    def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None: ...

    def is_locked(self, recommendation_id: str) -> bool: ...

    def load_acknowledged_decisions(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        as_of: datetime,
    ) -> tuple[AcknowledgedDecisionEvidence, ...]: ...

    def list_pending_recommendations(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        decision_type: str,
    ) -> tuple[RecommendationRecord, ...]: ...

    def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool: ...


class AsyncStateRepository(Protocol):
    async def initialize(self) -> None: ...

    async def save_league_snapshot(self, snapshot: LeagueSnapshotRecord) -> None: ...

    async def load_league_snapshot(
        self,
        league_id: str,
        fantasy_week: int,
    ) -> LeagueSnapshotRecord | None: ...

    async def save_data_freshness(self, freshness: DataFreshnessRecord) -> None: ...

    async def load_data_freshness(self, resource: str) -> DataFreshnessRecord | None: ...

    async def create_recommendation(self, recommendation: RecommendationRecord) -> bool: ...

    async def get_recommendation(self, recommendation_id: str) -> RecommendationRecord | None: ...

    async def record_delivery_attempt(self, attempt: DeliveryAttemptRecord) -> None: ...

    async def next_delivery_attempt_number(self, recommendation_id: str) -> int: ...

    async def has_successful_delivery(self, recommendation_id: str) -> bool: ...

    async def claim_delivery(
        self,
        recommendation_id: str,
        claimed_at: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> bool: ...

    async def release_delivery_claim(self, recommendation_id: str) -> bool: ...

    async def create_action_token(self, token: ActionTokenRecord) -> None: ...

    async def consume_action_token(
        self,
        token_hash: str,
        action: AcknowledgementAction,
        acknowledged_at: datetime,
    ) -> AcknowledgementResult: ...

    async def expire_recommendations(self, now: datetime) -> int: ...

    async def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None: ...

    async def is_locked(self, recommendation_id: str) -> bool: ...

    async def load_acknowledged_decisions(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        as_of: datetime,
    ) -> tuple[AcknowledgedDecisionEvidence, ...]: ...

    async def list_pending_recommendations(
        self,
        league_id: str,
        fantasy_week: int,
        *,
        decision_type: str,
    ) -> tuple[RecommendationRecord, ...]: ...

    async def supersede_recommendation(self, recommendation_id: str, now: datetime) -> bool: ...


class AsyncRuntimeStateRepository(AsyncStateRepository, AsyncNBADataCache, Protocol):
    async def load_runtime_policy(self) -> RuntimePolicyRecord | None: ...

    async def save_runtime_policy(self, policy: RuntimePolicyRecord) -> None: ...

    async def save_projection_observations(
        self, observations: tuple[ProjectionObservationRecord, ...]
    ) -> None: ...

    async def load_projection_observations(
        self,
        history_version: str,
        *,
        before: datetime | None = None,
        page_size: int = PROJECTION_OBSERVATION_PAGE_SIZE,
    ) -> tuple[ProjectionObservationRecord, ...]: ...

    async def upsert_scheduled_work(self, work: ScheduledWorkRecord) -> None: ...

    async def claim_due_work(
        self,
        now: datetime,
        *,
        correlation_id: str,
        lease_duration: timedelta = DEFAULT_SCHEDULED_WORK_LEASE,
        limit: int = 100,
    ) -> tuple[ScheduledWorkRecord, ...]: ...

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
    ) -> bool: ...

    async def list_scheduled_work(
        self,
        *,
        kind: DueWorkKind | None = None,
        statuses: tuple[ScheduledWorkStatus, ...] = (),
    ) -> tuple[ScheduledWorkRecord, ...]: ...

    async def cancel_expired_scheduled_work(self, now: datetime) -> int: ...
