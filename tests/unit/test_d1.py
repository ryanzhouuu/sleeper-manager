import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    ActionTokenRecord,
    DataFreshnessRecord,
    LeagueSnapshotRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from sleeper_manager.persistence.tokens import hash_action_token

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


class FakeStatement:
    def __init__(self, database: "FakeD1", query: str, params: tuple[Any, ...] = ()) -> None:
        self.database = database
        self.query = query
        self.params = params

    def bind(self, *params: object) -> "FakeStatement":
        return FakeStatement(self.database, self.query, params)

    def execute(self) -> sqlite3.Cursor:
        return self.database.connection.execute(self.query, self.params)

    async def run(self) -> dict[str, Any]:
        cursor = self.execute()
        self.database.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}

    async def first(self) -> dict[str, Any] | None:
        cursor = self.execute()
        row = cursor.fetchone()
        return dict(row) if row is not None else None


class FakeD1:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row

    def prepare(self, query: str) -> FakeStatement:
        return FakeStatement(self, query)

    async def exec(self, query: str) -> None:
        self.connection.executescript(query)

    async def batch(self, statements: list[FakeStatement]) -> list[dict[str, Any]]:
        self.connection.execute("BEGIN")
        results: list[dict[str, Any]] = []
        try:
            for statement in statements:
                cursor = statement.execute()
                results.append({"meta": {"changes": cursor.rowcount}})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results


def recommendation() -> RecommendationRecord:
    return RecommendationRecord(
        recommendation_id="recommendation-1",
        idempotency_key="league:1:player:game:lock_in:policy",
        league_id="league-1",
        fantasy_week=1,
        player_id="player-1",
        game_id="game-1",
        decision_type="placeholder_lock_in",
        title="Lock player",
        message="Lock the placeholder player",
        deadline=NOW + timedelta(hours=1),
        policy_version="policy-1",
        created_at=NOW,
    )


def make_repository() -> tuple[FakeD1, D1StateRepository]:
    database = FakeD1()
    asyncio.run(database.exec(D1_SCHEMA))
    return database, D1StateRepository(database)


def test_d1_repository_persists_records_and_consumes_token_once() -> None:
    _, repository = make_repository()
    record = recommendation()
    asyncio.run(repository.create_recommendation(record))
    raw_token = "test-token"
    asyncio.run(
        repository.create_action_token(
            ActionTokenRecord(
                token_hash=hash_action_token(raw_token),
                recommendation_id=record.recommendation_id,
                action=AcknowledgementAction.LOCKED,
                created_at=NOW,
                expires_at=record.deadline or NOW,
            )
        )
    )

    result = asyncio.run(
        repository.consume_action_token(
            hash_action_token(raw_token),
            AcknowledgementAction.LOCKED,
            NOW + timedelta(minutes=1),
        )
    )
    replay = asyncio.run(
        repository.consume_action_token(
            hash_action_token(raw_token),
            AcknowledgementAction.LOCKED,
            NOW + timedelta(minutes=2),
        )
    )

    assert result.outcome is AcknowledgementOutcome.APPLIED
    assert replay.outcome is AcknowledgementOutcome.ALREADY_USED
    assert asyncio.run(repository.is_locked(record.recommendation_id))


def test_d1_repository_round_trips_snapshot_and_freshness() -> None:
    _, repository = make_repository()
    snapshot = LeagueSnapshotRecord(
        snapshot_id="snapshot-1",
        league_id="league-1",
        fantasy_week=1,
        payload_json='{"roster": []}',
        retrieved_at=NOW,
    )
    freshness = DataFreshnessRecord(
        resource="scoreboard",
        retrieved_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        quality=DataQualityState.FRESH,
        warnings=("warning",),
        errors=(),
    )

    asyncio.run(repository.save_league_snapshot(snapshot))
    asyncio.run(repository.save_data_freshness(freshness))

    assert asyncio.run(repository.load_league_snapshot("league-1", 1)) == snapshot
    assert asyncio.run(repository.load_data_freshness("scoreboard")) == freshness
