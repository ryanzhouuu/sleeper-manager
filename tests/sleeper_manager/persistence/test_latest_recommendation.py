from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import RecommendationRecord, RecommendationStatus
from sleeper_manager.persistence.sqlite import SQLiteStateRepository

NOW = datetime(2026, 10, 19, 15, tzinfo=UTC)


def _record(
    recommendation_id: str,
    *,
    created_at: datetime,
    status: RecommendationStatus = RecommendationStatus.PENDING,
    decision_type: str = "weekly_lineup",
    title: str = "Lineup is set",
) -> RecommendationRecord:
    return RecommendationRecord(
        recommendation_id=recommendation_id,
        idempotency_key=recommendation_id,
        league_id="league-1",
        fantasy_week=3,
        player_id="player-1",
        game_id=None,
        decision_type=decision_type,
        title=title,
        message="Stored message",
        deadline=NOW + timedelta(days=1),
        policy_version="policy-1",
        created_at=created_at,
        status=status,
        trace_json="{}",
    )


def test_latest_current_recommendation_skips_newer_superseded_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    current = _record("current", created_at=NOW, title="Lineup is set")
    newer_superseded = _record(
        "newer",
        created_at=NOW + timedelta(minutes=5),
        status=RecommendationStatus.SUPERSEDED,
        title="Lineup move required",
    )
    other_kind = _record(
        "lock",
        created_at=NOW + timedelta(minutes=10),
        decision_type="lock",
        title="Lock player",
    )
    repository.create_recommendation(current)
    repository.create_recommendation(newer_superseded)
    repository.create_recommendation(other_kind)

    found = repository.latest_current_recommendation("league-1", 3, decision_type="weekly_lineup")

    assert found is not None
    assert found.recommendation_id == "current"
    assert found.title == "Lineup is set"


def test_latest_current_recommendation_is_none_when_only_superseded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()
    repository.create_recommendation(
        _record("old", created_at=NOW, status=RecommendationStatus.SUPERSEDED)
    )

    assert (
        repository.latest_current_recommendation("league-1", 3, decision_type="weekly_lineup")
        is None
    )


def test_async_sqlite_delegates_latest_current_recommendation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    import asyncio

    asyncio.run(repository.initialize())
    record = _record("current", created_at=NOW)
    assert asyncio.run(repository.create_recommendation(record))
    found = asyncio.run(
        repository.latest_current_recommendation("league-1", 3, decision_type="weekly_lineup")
    )
    assert found == replace(record, revision=1)
