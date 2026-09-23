"""Verify a scheduled capture writes receipts without rereading the catalog."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.persistence.forecast_sqlite import AsyncSQLiteForecastArchiveRepository
from sleeper_manager.workflows.forecast_collection import capture_scheduled_forecast
from tests.paths import FIXTURES_DIR
from tests.sleeper_manager.workflows.test_forecast_tipoffs import MemoryCache

NOW = datetime(2026, 9, 22, 13, tzinfo=UTC)
FIXTURE = (FIXTURES_DIR / "sleeper" / "season_forecasts.json").read_bytes()
SLEEPER = FIXTURES_DIR / "sleeper"


class RawResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.content = body


class FixtureSleeper:
    """Serve the checked-in league fixtures and count catalog reads."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.player_calls = 0

    async def league(self, league_id: str) -> dict[str, Any]:
        payload = _fixture("current_league.json")
        payload["league_id"] = league_id
        payload["status"] = self.status
        return payload

    async def users(self, league_id: str) -> list[dict[str, Any]]:
        del league_id
        return _fixture("users.json")

    async def rosters(self, league_id: str) -> list[dict[str, Any]]:
        del league_id
        return _fixture("rosters.json")

    async def state(self) -> dict[str, Any]:
        return _fixture("state.json")

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        del league_id, week
        return _fixture("transactions.json")

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        del league_id, week
        return [
            {"roster_id": 1, "matchup_id": 1, "players": ["player-1", "player-2", "player-3"]},
            {"roster_id": 2, "matchup_id": 1, "players": ["player-4", "player-5"]},
        ]

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]:
        del active
        self.player_calls += 1
        return {
            player_id: {"full_name": f"Player {index}", "team": "CHI", "espn_id": index}
            for index, player_id in enumerate(
                ("player-1", "player-2", "player-3", "player-4", "player-5"),
                start=1,
            )
        }


class CountingNba:
    def __init__(self) -> None:
        self.roster_calls = 0

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        self.roster_calls += 1
        players = tuple(
            ProviderPlayer(
                provider_id=str(index),
                full_name=f"Player {index}",
                team_id="espn-chi",
                team_abbreviation=team_id,
                active=True,
                source=SourceMetadata("espn", f"player-{index}", NOW),
            )
            for index in range(1, 6)
        )
        return ProviderResult(players, _quality("roster"))

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        del team_id, season
        game = ScheduledGame(
            provider_id="future",
            start_time=NOW + timedelta(days=1),
            status=GameStatus.SCHEDULED,
            home_team_id="espn-chi",
            away_team_id="espn-ny",
            status_detail=None,
            source=SourceMetadata("espn", "future", NOW),
        )
        return ProviderResult((game,), _quality("schedule"))


def test_preseason_capture_stores_a_daily_receipt_without_the_catalog(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A daily snapshot does not need opponent games before the season."""

    archive = _archive(tmp_path)
    sleeper = FixtureSleeper("pre_draft")
    fetches = 0

    async def fetch(url: str) -> RawResponse:
        nonlocal fetches
        del url
        fetches += 1
        return RawResponse(FIXTURE)

    asyncio.run(
        capture_scheduled_forecast(
            archive,
            MemoryCache(),
            sleeper,
            policy=default_runtime_policy(history_version="history-v1"),
            nba=CountingNba(),
            fetch=fetch,
            now=NOW,
            league_id="league-current",
            user_id="user-manager",
        )
    )

    assert sleeper.player_calls == 0
    assert fetches == 1
    assert asyncio.run(archive.measure_storage()).receipt_count == 1


def test_in_season_capture_reads_the_catalog_once_per_local_day(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Reuse cached tipoffs on the next wake and do not repeat a finished slot."""

    archive = _archive(tmp_path)
    sleeper = FixtureSleeper("in_season")
    nba = CountingNba()
    cache = MemoryCache()
    fetches = 0

    async def fetch(url: str) -> RawResponse:
        nonlocal fetches
        del url
        fetches += 1
        return RawResponse(FIXTURE)

    async def exercise() -> None:
        for _ in range(2):
            await capture_scheduled_forecast(
                archive,
                cache,
                sleeper,
                policy=default_runtime_policy(history_version="history-v1"),
                nba=nba,
                fetch=fetch,
                now=NOW,
                league_id="league-current",
                user_id="user-manager",
            )

    asyncio.run(exercise())

    assert sleeper.player_calls == 1
    assert nba.roster_calls == 1
    assert fetches == 1
    assert asyncio.run(archive.measure_storage()).receipt_count == 1


def test_tipoff_provider_failure_still_captures_daily_forecast(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """An NBA lookup gap is reported without preventing the independent daily slot."""

    class FailingNba(CountingNba):
        async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
            del team_id
            raise RuntimeError("private provider response")

    archive = _archive(tmp_path)
    sleeper = FixtureSleeper("in_season")
    cache = MemoryCache()
    fetches = 0

    async def fetch(url: str) -> RawResponse:
        nonlocal fetches
        del url
        fetches += 1
        return RawResponse(FIXTURE)

    asyncio.run(
        capture_scheduled_forecast(
            archive,
            cache,
            sleeper,
            policy=default_runtime_policy(history_version="history-v1"),
            nba=FailingNba(),
            fetch=fetch,
            now=NOW,
            league_id="league-current",
            user_id="user-manager",
        )
    )

    assert fetches == 1
    assert cache.records == {}
    assert asyncio.run(archive.measure_storage()).receipt_count == 1
    assert capsys.readouterr().err == "Forecast tipoff lookup failed: RuntimeError\n"


def _archive(tmp_path) -> AsyncSQLiteForecastArchiveRepository:  # type: ignore[no-untyped-def]
    archive = AsyncSQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    asyncio.run(archive.initialize())
    return archive


def _fixture(name: str) -> Any:
    return json.loads((SLEEPER / name).read_text(encoding="utf-8"))


def _quality(resource: str) -> DataQualityReport:
    return DataQualityReport(
        state=DataQualityState.FRESH,
        resource=resource,
        record_count=1,
        retrieved_at=NOW,
        source_updated_at=None,
        expires_at=None,
    )
