"""Verify capture-time roster, eligibility, and fantasy-week game snapshots."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sleeper_manager.domain.forecast_context import ForecastContextGapCode
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
from sleeper_manager.workflows.forecast_context_collection import (
    bind_forecast_context,
    collect_forecast_context,
    observe_forecast_context,
)

READ_AT = datetime(2026, 9, 22, 18, tzinfo=UTC)
IN_WEEK = datetime(2026, 9, 24, 23, tzinfo=UTC)
IN_WEEK_FINAL = datetime(2026, 9, 22, 0, tzinfo=UTC)
NEXT_WEEK = datetime(2026, 9, 29, 18, tzinfo=UTC)


def test_collection_keeps_week_games_and_roster_order() -> None:
    """Store both final and scheduled games inside the current fantasy week."""

    observation = asyncio.run(
        collect_forecast_context(
            _Nba(),
            profile=_profile(),
            matchups=_matchups(),
            catalog=_catalog(),
            mapping_overrides={},
            read_at=READ_AT,
        )
    )
    stored = bind_forecast_context(observation, receipt_id="receipt-1", persisted_at=READ_AT)

    assert stored.week == 1
    assert stored.matchup_id == 4
    assert stored.bye is False
    assert stored.manager is not None
    assert stored.manager.player_ids == ("manager", "reserve")
    assert stored.manager.starter_ids == ("manager", None)
    assert stored.manager.reserve_ids == ("reserve",)
    assert stored.opponent is not None
    assert stored.opponent.roster_id == 2
    assert [(item.player_id, item.positions) for item in stored.eligibility] == [
        ("manager", ("PG", "SG")),
        ("opponent", ("C",)),
        ("reserve", ()),
    ]
    assert [(game.game_id, game.status) for game in stored.games] == [
        ("final-game", GameStatus.FINAL),
        ("week-game", GameStatus.SCHEDULED),
    ]
    assert stored.gaps == ()


def test_bye_snapshot_does_not_invent_an_opponent() -> None:
    """A missing matchup id is a bye, not a failed opponent read."""

    matchups = [
        {"roster_id": 1, "matchup_id": None, "players": ["manager", "reserve"]},
    ]
    observation = asyncio.run(
        collect_forecast_context(
            _Nba(),
            profile=_profile(),
            matchups=matchups,
            catalog=_catalog(),
            mapping_overrides={},
            read_at=READ_AT,
        )
    )
    stored = bind_forecast_context(observation, receipt_id="receipt-bye", persisted_at=READ_AT)

    assert stored.bye is True
    assert stored.matchup_id is None
    assert stored.opponent is None
    assert ForecastContextGapCode.MATCHUP not in {gap.code for gap in stored.gaps}
    assert ForecastContextGapCode.ROSTER not in {gap.code for gap in stored.gaps}


def test_unmapped_player_and_failed_schedule_are_explicit_gaps() -> None:
    """Do not guess a team or a slate when identity or schedule evidence is missing."""

    catalog = _catalog()
    catalog["stranger"] = {"full_name": "Nobody Known", "team": "NYK", "fantasy_positions": ["PF"]}
    profile = _profile(
        rosters=(
            Roster(1, "user-1", ("manager", "stranger"), ("manager", None), ("stranger",)),
            Roster(2, "user-2", ("opponent",), ("opponent",)),
        )
    )

    class FailingSchedule(_Nba):
        async def team_schedule(
            self, team_id: str, season: int
        ) -> ProviderResult[tuple[ScheduledGame, ...]]:
            del team_id, season
            return ProviderResult((), _quality("schedule", DataQualityState.ERROR))

    observation = asyncio.run(
        collect_forecast_context(
            FailingSchedule(),
            profile=profile,
            matchups=_matchups(),
            catalog=catalog,
            mapping_overrides={},
            read_at=READ_AT,
        )
    )
    stored = bind_forecast_context(observation, receipt_id="receipt-gap", persisted_at=READ_AT)

    assert stored.games == ()
    assert _gap(stored.gaps, ForecastContextGapCode.IDENTITY, "stranger")
    assert _gap(stored.gaps, ForecastContextGapCode.SCHEDULE, "espn-chi")
    assert "manager" in {item.player_id for item in stored.eligibility}


def test_failed_matchup_read_keeps_the_manager_roster_and_hides_the_payload() -> None:
    """A companion failure still explains itself without copying the exception text."""

    class Boom:
        async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
            del league_id, week
            raise RuntimeError("secret payload")

        async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]:
            del active
            return _catalog()

    observation = asyncio.run(
        observe_forecast_context(
            Boom(),
            _Nba(),
            profile=_profile(),
            mapping_overrides={},
            read_at=READ_AT,
        )
    )
    stored = bind_forecast_context(observation, receipt_id="receipt-matchup", persisted_at=READ_AT)

    assert stored.manager is not None
    assert stored.manager.roster_id == 1
    assert stored.opponent is None
    assert _gap(stored.gaps, ForecastContextGapCode.MATCHUP, "league-1")
    assert all("secret" not in gap.detail for gap in stored.gaps)


def _gap(gaps: tuple[Any, ...], code: ForecastContextGapCode, subject: str) -> bool:
    """Report whether one companion gap names the expected subject."""

    return any(gap.code is code and gap.subject == subject for gap in gaps)


def _profile(*, rosters: tuple[Roster, ...] | None = None) -> LeagueProfile:
    """Build the two-roster league used by snapshot collection."""

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
        rosters=rosters
        or (
            Roster(1, "user-1", ("manager", "reserve"), ("manager", None), ("reserve",)),
            Roster(2, "user-2", ("opponent",), ("opponent",)),
        ),
        manager_user_id="user-1",
        manager_roster_id=1,
        fantasy_week=FantasyWeek(1, "2026", "regular"),
        transactions=(),
        configuration_fingerprint="fingerprint",
        retrieved_at=READ_AT,
    )


def _matchups() -> list[dict[str, Any]]:
    """Return one head-to-head matchup for the fixture rosters."""

    return [
        {"roster_id": 1, "matchup_id": 4, "players": ["manager", "reserve"]},
        {"roster_id": 2, "matchup_id": 4, "players": ["opponent"]},
    ]


def _catalog() -> dict[str, dict[str, Any]]:
    """Return fantasy positions and stable ESPN ids for the fixture players."""

    return {
        "manager": {
            "full_name": "Manager Player",
            "team": "CHI",
            "espn_id": 10,
            "fantasy_positions": ["PG", " PG ", "SG"],
        },
        "reserve": {"full_name": "Reserve Player", "team": "CHI", "espn_id": 11},
        "opponent": {
            "full_name": "Opponent Player",
            "team": "CHI",
            "espn_id": 12,
            "fantasy_positions": ["C"],
        },
    }


class _Nba:
    """Serve one mapped Chicago roster and a schedule that spans two fantasy weeks."""

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        players = tuple(
            ProviderPlayer(
                provider_id=provider_id,
                full_name=name,
                team_id="espn-chi",
                team_abbreviation=team_id,
                active=True,
                source=_source(provider_id),
            )
            for provider_id, name in (
                ("10", "Manager Player"),
                ("11", "Reserve Player"),
                ("12", "Opponent Player"),
            )
        )
        return ProviderResult(players, _quality("roster", DataQualityState.FRESH))

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        del team_id, season
        games = tuple(
            ScheduledGame(
                provider_id=provider_id,
                start_time=start,
                status=status,
                home_team_id="espn-chi",
                away_team_id="espn-ny",
                status_detail=None,
                source=_source(provider_id),
            )
            for provider_id, start, status in (
                ("before-week", READ_AT - timedelta(days=2), GameStatus.FINAL),
                ("final-game", IN_WEEK_FINAL, GameStatus.FINAL),
                ("week-game", IN_WEEK, GameStatus.SCHEDULED),
                ("next-week", NEXT_WEEK, GameStatus.SCHEDULED),
            )
        )
        return ProviderResult(games, _quality("schedule", DataQualityState.FRESH))


def _source(provider_id: str) -> SourceMetadata:
    """Build source metadata stamped at the capture read."""

    return SourceMetadata("espn", provider_id, READ_AT)


def _quality(resource: str, state: DataQualityState) -> DataQualityReport:
    """Build a provider quality report without embedding response bodies."""

    return DataQualityReport(
        state=state,
        resource=resource,
        record_count=1,
        retrieved_at=READ_AT,
        source_updated_at=None,
        expires_at=None,
    )
