from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sleeper_manager.domain.nba import DataQualityState


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

    def has_successful_delivery(self, recommendation_id: str) -> bool: ...

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

    async def has_successful_delivery(self, recommendation_id: str) -> bool: ...

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
