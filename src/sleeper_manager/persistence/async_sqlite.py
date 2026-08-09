from datetime import datetime
from pathlib import Path

from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementResult,
    ActionTokenRecord,
    AsyncStateRepository,
    DataFreshnessRecord,
    DeliveryAttemptRecord,
    LeagueSnapshotRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.sqlite import SQLiteStateRepository


class AsyncSQLiteStateRepository(AsyncStateRepository):
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

    async def has_successful_delivery(self, recommendation_id: str) -> bool:
        return self._repository.has_successful_delivery(recommendation_id)

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
