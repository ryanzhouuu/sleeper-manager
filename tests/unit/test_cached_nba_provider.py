from asyncio import run
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    DataQualityReport,
    DataQualityState,
    GameStatus,
    PlayerAvailability,
    ProviderPlayer,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.integrations.nba.cached_provider import CachedNBAProvider
from sleeper_manager.persistence.base import CachedNBARecord
from sleeper_manager.persistence.nba_cache import SQLiteNBADataCache

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)


def _source(retrieved_at: datetime) -> SourceMetadata:
    return SourceMetadata(provider="espn", provider_id="x", retrieved_at=retrieved_at)


def _quality(state: DataQualityState = DataQualityState.FRESH) -> DataQualityReport:
    return DataQualityReport(
        state=state,
        resource="fixture",
        record_count=1,
        retrieved_at=NOW,
        source_updated_at=None,
        expires_at=NOW + timedelta(hours=1),
    )


class _CountingNBA:
    def __init__(self) -> None:
        self.roster_calls = 0
        self.schedule_calls = 0
        self.injury_calls = 0
        self.injury_error: Exception | None = None

    async def scoreboard(self, game_date):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def game_summary(self, game_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def team_roster(self, team_id: str):
        self.roster_calls += 1
        return _Result(
            (ProviderPlayer("401", "Point Guard One", "12", "chi", True, _source(NOW)),),
            _quality(),
        )

    async def team_schedule(self, team_id: str, season: int):
        self.schedule_calls += 1
        return _Result(
            (
                ScheduledGame(
                    provider_id="g1",
                    start_time=NOW + timedelta(hours=2),
                    status=GameStatus.SCHEDULED,
                    home_team_id="12",
                    away_team_id="14",
                    status_detail=None,
                    source=_source(NOW),
                ),
            ),
            _quality(),
        )

    async def injuries(self):
        self.injury_calls += 1
        if self.injury_error is not None:
            raise self.injury_error
        return _Result(
            (
                PlayerAvailability(
                    player_id="401",
                    status=AvailabilityStatus.QUESTIONABLE,
                    detail="ankle",
                    source=_source(NOW),
                ),
            ),
            _quality(DataQualityState.PARTIAL),
        )


class _Result:
    def __init__(self, records, quality) -> None:  # noqa: ANN001
        self.records = records
        self.quality = quality


def _provider(tmp_path: Path) -> tuple[CachedNBAProvider, _CountingNBA]:
    cache = SQLiteNBADataCache(tmp_path / "cache.db")
    cache.initialize()
    inner = _CountingNBA()
    return CachedNBAProvider(inner, cache, clock=lambda: NOW), inner


async def _round_trip(tmp_path: Path):  # noqa: ANN201
    provider, inner = _provider(tmp_path)

    roster_first = await provider.team_roster("12")
    schedule_first = await provider.team_schedule("12", 2026)
    injuries_first = await provider.injuries()

    roster_second = await provider.team_roster("12")
    schedule_second = await provider.team_schedule("12", 2026)
    injuries_second = await provider.injuries()
    return (
        (roster_first, roster_second),
        (schedule_first, schedule_second),
        (injuries_first, injuries_second),
        inner,
    )


def test_cache_hits_skip_the_provider_and_round_trip_records(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (roster_a, roster_b), (schedule_a, schedule_b), (inj_a, inj_b), inner = run(
        _round_trip(tmp_path)
    )

    assert (inner.roster_calls, inner.schedule_calls, inner.injury_calls) == (1, 1, 1)

    assert list(roster_b.records) == list(roster_a.records)
    assert roster_b.quality.state is DataQualityState.FRESH
    assert roster_b.quality.retrieved_at == roster_a.quality.retrieved_at

    assert list(schedule_b.records) == list(schedule_a.records)
    assert schedule_b.records[0].start_time == schedule_a.records[0].start_time
    assert schedule_b.records[0].source == schedule_a.records[0].source

    assert list(inj_b.records) == list(inj_a.records)
    assert inj_b.records[0].status is AvailabilityStatus.QUESTIONABLE
    assert inj_b.quality.state is DataQualityState.PARTIAL


def _seed_expired_injuries(cache: SQLiteNBADataCache) -> None:
    cache.put(
        CachedNBARecord(
            cache_key="nba:injuries",
            provider="nba",
            resource="injuries",
            schema_version="1",
            payload_json=(
                '[{"player_id":"401","status":"questionable","detail":"ankle",'
                '"source":{"provider":"espn","provider_id":"x",'
                f'"retrieved_at":"{NOW.isoformat()}","source_updated_at":null,'
                '"schema_version":"1","content_hash":null}}]'
            ),
            retrieved_at=NOW,
            source_updated_at=None,
            expires_at=NOW - timedelta(minutes=1),
            quality=DataQualityState.FRESH,
        )
    )


def test_expired_cache_entries_trigger_provider_refresh(tmp_path) -> None:
    cache = SQLiteNBADataCache(tmp_path / "cache.db")
    cache.initialize()
    _seed_expired_injuries(cache)
    inner = _CountingNBA()
    provider = CachedNBAProvider(inner, cache, clock=lambda: NOW + timedelta(minutes=2))

    result = run(provider.injuries())

    assert inner.injury_calls == 1
    assert result.quality.state is DataQualityState.PARTIAL


def test_expired_cache_refresh_errors_propagate(tmp_path) -> None:
    cache = SQLiteNBADataCache(tmp_path / "cache.db")
    cache.initialize()
    _seed_expired_injuries(cache)
    inner = _CountingNBA()
    inner.injury_error = RuntimeError("injury endpoint unavailable")
    provider = CachedNBAProvider(inner, cache, clock=lambda: NOW + timedelta(minutes=2))

    with pytest.raises(RuntimeError, match="injury endpoint unavailable"):
        run(provider.injuries())

    assert inner.injury_calls == 1
