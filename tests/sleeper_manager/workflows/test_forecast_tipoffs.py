"""Verify tipoff caching, matchup selection, and roster-to-game resolution."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.league import (
    FantasyWeek,
    LeagueMode,
    LeagueProfile,
    LeagueUser,
    RosterSlot,
)
from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.persistence.base import CachedNBARecord
from sleeper_manager.persistence.rows import cached_nba_as_of
from sleeper_manager.workflows.forecast_tipoffs import (
    ForecastTipoffError,
    capture_roster_player_ids,
    opponent_roster_id,
    parse_matchup_sides,
    resolve_relevant_tipoffs,
    tipoffs_for_capture,
)

NOW = datetime(2026, 9, 22, 18, tzinfo=UTC)
TIPOFF = datetime(2026, 9, 22, 23, tzinfo=UTC)


class MemoryCache:
    """In-memory NBA cache that applies the production freshness rule."""

    def __init__(self) -> None:
        self.records: dict[str, CachedNBARecord] = {}

    async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        record = self.records.get(cache_key)
        if record is None:
            return None
        return cached_nba_as_of(record, now)

    async def put(self, record: CachedNBARecord) -> None:
        self.records[record.cache_key] = record


class GuardCache(MemoryCache):
    async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
        del cache_key, now
        raise AssertionError("offseason capture must not read the tipoff cache")


def test_second_wake_reuses_cached_tipoffs_without_resolving_again() -> None:
    """Download the slate once per local day."""

    cache = MemoryCache()
    calls = 0

    async def resolve() -> tuple[datetime, ...]:
        nonlocal calls
        calls += 1
        return (TIPOFF,)

    async def exercise() -> None:
        first = await tipoffs_for_capture(
            cache,
            league_id="league-1",
            now=NOW,
            timezone_name="UTC",
            in_season=True,
            resolve=resolve,
        )
        second = await tipoffs_for_capture(
            cache,
            league_id="league-1",
            now=NOW + timedelta(minutes=5),
            timezone_name="UTC",
            in_season=True,
            resolve=resolve,
        )
        assert first == second == (TIPOFF,)

    asyncio.run(exercise())
    assert calls == 1


def test_failed_lookup_is_not_cached() -> None:
    """Leave the next wake free to retry a failed roster or schedule read."""

    cache = MemoryCache()

    async def resolve() -> tuple[datetime, ...]:
        raise ForecastTipoffError("catalog unavailable")

    with pytest.raises(ForecastTipoffError, match="catalog"):
        asyncio.run(
            tipoffs_for_capture(
                cache,
                league_id="league-1",
                now=NOW,
                timezone_name="UTC",
                in_season=True,
                resolve=resolve,
            )
        )

    assert cache.records == {}


def test_offseason_does_not_touch_the_tipoff_cache() -> None:
    """Daily captures outside the season do not need game times."""

    async def resolve() -> tuple[datetime, ...]:
        raise AssertionError("offseason capture must not resolve tipoffs")

    result = asyncio.run(
        tipoffs_for_capture(
            GuardCache(),
            league_id="league-1",
            now=NOW,
            timezone_name="UTC",
            in_season=False,
            resolve=resolve,
        )
    )

    assert result == ()


def test_matchup_selects_the_opponent_roster() -> None:
    """Use the shared matchup id and treat a null id as a bye."""

    sides = parse_matchup_sides(
        [
            {"roster_id": 1, "matchup_id": 4, "players": ["manager"]},
            {"roster_id": 2, "matchup_id": 4, "players": ["opponent"]},
        ]
    )
    bye = parse_matchup_sides([{"roster_id": 1, "matchup_id": None, "players": ["manager"]}])

    assert opponent_roster_id(sides, 1) == 2
    assert opponent_roster_id(bye, 1) is None
    assert capture_roster_player_ids(
        _profile(),
        [
            {"roster_id": 1, "matchup_id": 4, "players": ["manager"]},
            {"roster_id": 2, "matchup_id": 4, "players": ["opponent"]},
        ],
    ) == ("manager", "opponent")
    with pytest.raises(ForecastTipoffError, match="ambiguous"):
        opponent_roster_id(
            parse_matchup_sides(
                [
                    {"roster_id": 1, "matchup_id": 4, "players": ["manager"]},
                    {"roster_id": 2, "matchup_id": 4, "players": ["one"]},
                    {"roster_id": 3, "matchup_id": 4, "players": ["two"]},
                ]
            ),
            1,
        )


def test_resolve_uses_scheduled_games_for_mapped_teams() -> None:
    """Keep final games out of the pre-tipoff slate."""

    tipoffs = asyncio.run(
        resolve_relevant_tipoffs(
            player_ids=("manager",),
            catalog={"manager": {"full_name": "Manager Player", "team": "CHI", "espn_id": 10}},
            nba=_Nba(),
            season="2026",
            mapping_overrides={},
        )
    )

    assert tipoffs == (TIPOFF,)


def _profile() -> LeagueProfile:
    """Build the two-roster league used by matchup selection."""

    return LeagueProfile(
        league_id="league-1",
        name="Fixture League",
        sport="nba",
        season="2026",
        season_type="regular",
        status="in_season",
        total_rosters=2,
        previous_league_id=None,
        mode=LeagueMode.LOCK_IN,
        roster_slots=(RosterSlot(index=0, position="UTIL", is_starting=True),),
        scoring=ScoringPolicy(points=1),
        users=(LeagueUser("user-1", None, None, None),),
        rosters=(
            Roster(1, "user-1", ("manager",), ("manager",)),
            Roster(2, "user-2", ("opponent",), ("opponent",)),
        ),
        manager_user_id="user-1",
        manager_roster_id=1,
        fantasy_week=FantasyWeek(1, "2026", "regular"),
        transactions=(),
        configuration_fingerprint="fingerprint",
        retrieved_at=NOW,
    )


class _Nba:
    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        player = ProviderPlayer(
            provider_id="10",
            full_name="Manager Player",
            team_id="espn-chi",
            team_abbreviation=team_id,
            active=True,
            source=_source("roster"),
        )
        return ProviderResult((player,), _quality("roster"))

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        del team_id, season
        scheduled = ScheduledGame(
            provider_id="game-1",
            start_time=TIPOFF,
            status=GameStatus.SCHEDULED,
            home_team_id="espn-chi",
            away_team_id="espn-ny",
            status_detail=None,
            source=_source("schedule"),
        )
        final = ScheduledGame(
            provider_id="game-2",
            start_time=TIPOFF - timedelta(days=1),
            status=GameStatus.FINAL,
            home_team_id="espn-chi",
            away_team_id="espn-bos",
            status_detail=None,
            source=_source("schedule-final"),
        )
        return ProviderResult((scheduled, final), _quality("schedule"))


def _source(provider_id: str) -> SourceMetadata:
    return SourceMetadata("espn", provider_id, NOW)


def _quality(resource: str) -> DataQualityReport:
    return DataQualityReport(
        state=DataQualityState.FRESH,
        resource=resource,
        record_count=1,
        retrieved_at=NOW,
        source_updated_at=None,
        expires_at=None,
    )
