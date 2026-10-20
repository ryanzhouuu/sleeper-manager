"""Load the public GET /home document from Sleeper, ESPN, and D1.

Sleeper supplies the roster and matchup. ESPN supplies this week's games.
The recommendation store supplies the current weekly lineup text. Cache misses
and cache write failures still return a live document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from sleeper_manager.cloudflare.home_cache import decode_schedule, read_home_cache, write_home_cache
from sleeper_manager.cloudflare.home_document import (
    GameRow,
    HomeIdentity,
    HomeSide,
    RosterRow,
    assemble_home,
    avatar_image_url,
    label_team_games,
    plan_status_from_trace,
)
from sleeper_manager.cloudflare.home_identities import (
    fill_espn_ids,
    load_player_identities,
    remember_identities,
)
from sleeper_manager.cloudflare.home_roster import matchup_sides, roster_rows
from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.cloudflare.routes import RouteResponse
from sleeper_manager.domain.nba import (
    DataQualityState,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
)
from sleeper_manager.domain.nba_season import espn_season_end_year
from sleeper_manager.domain.scheduling import eastern_fantasy_week, manager_local_day
from sleeper_manager.integrations.nba.mapping import normalize_team
from sleeper_manager.integrations.sleeper.schemas import (
    SleeperLeaguePayload,
    SleeperRosterPayload,
    SleeperStatePayload,
    SleeperUserPayload,
)
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository

_WEEKLY_LINEUP = "weekly_lineup"
_SectionStatus = Literal["ready", "empty", "unavailable"]


class HomeSleeper(Protocol):
    """Sleeper reads used to build one home document."""

    async def league(self, league_id: str) -> dict[str, Any]: ...

    async def rosters(self, league_id: str) -> list[dict[str, Any]]: ...

    async def users(self, league_id: str) -> list[dict[str, Any]]: ...

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]: ...

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]: ...

    async def state(self) -> dict[str, Any]: ...


class HomeSchedule(Protocol):
    """ESPN team schedule read used to place roster games."""

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]: ...

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]: ...


async def build_home(
    *,
    sleeper: HomeSleeper,
    espn: HomeSchedule,
    repository: AsyncRuntimeStateRepository,
    league_id: str,
    user_id: str,
    timezone_name: str,
    now: datetime,
) -> RouteResponse:
    """Return the public document, or 503 when the league itself cannot be read."""
    if not league_id.strip() or not user_id.strip():
        return _league_unavailable()
    loaded = await _load_league(sleeper, league_id)
    if loaded is None:
        return _league_unavailable()
    league, users, raw_users, rosters, state = loaded
    week = _fantasy_week(state, league)
    if week is None:
        return _league_unavailable()
    owned = [roster for roster in rosters if roster.owner_id == user_id]
    if len(owned) != 1:
        return _league_unavailable()
    manager_roster = owned[0]
    try:
        season_year = espn_season_end_year(state.season or league.season)
    except (TypeError, ValueError):
        return _league_unavailable()
    matchup = await _matchup(
        sleeper,
        users,
        raw_users,
        rosters,
        manager_roster,
        league_id=league_id,
        user_id=user_id,
        week=week,
    )
    if matchup is None:
        return _league_unavailable()
    manager_side, opponent_side = matchup
    loaded_identities = await load_player_identities(
        sleeper, repository, timezone_name=timezone_name, now=now
    )
    if loaded_identities is None:
        return _league_unavailable()
    identities, from_cache = loaded_identities
    if not from_cache and await fill_espn_ids(espn, identities, manager_roster.players):
        await remember_identities(repository, identities, timezone_name=timezone_name, now=now)
    rows = roster_rows(
        league.roster_positions,
        manager_roster.starters,
        manager_roster.players,
        manager_roster.reserve or [],
        identities,
    )
    if rows is None:
        return _league_unavailable()
    games_status, games = await _games(
        espn,
        repository,
        rows,
        identities,
        season_year=season_year,
        timezone_name=timezone_name,
        now=now,
    )
    recommendation = await _recommendation(repository, league_id, week)
    return RouteResponse(
        200,
        assemble_home(
            league_name=league.name,
            week=week,
            timezone_name=timezone_name,
            manager=manager_side,
            opponent=opponent_side,
            roster=rows,
            games_status=games_status,
            games=games,
            recommendation_status=recommendation[0],
            recommendation_plan_status=recommendation[1],
            recommendation_title=recommendation[2],
            recommendation_message=recommendation[3],
            recommendation_deadline=recommendation[4],
        ),
    )


def _league_unavailable() -> RouteResponse:
    return RouteResponse(503, {"status": "league_unavailable"})


def _fantasy_week(state: SleeperStatePayload, league: SleeperLeaguePayload) -> int | None:
    week = next(
        (value for value in (state.week, state.leg, state.display_week) if value is not None),
        None,
    )
    if week is None or week < 0:
        return None
    season_type = state.season_type or league.season_type
    if week == 0 and season_type not in {"off", "pre"}:
        return None
    return week


def _display_name(user: SleeperUserPayload | None) -> str | None:
    if user is None:
        return None
    team_name = user.metadata.get("team_name")
    if isinstance(team_name, str) and team_name.strip():
        return team_name.strip()
    if user.display_name and user.display_name.strip():
        return user.display_name.strip()
    if user.username and user.username.strip():
        return user.username.strip()
    return None


def _user_avatar(raw_users: Sequence[Mapping[str, Any]], user_id: str) -> str | None:
    for raw in raw_users:
        if raw.get("user_id") != user_id:
            continue
        avatar = raw.get("avatar")
        if isinstance(avatar, str) and avatar.strip():
            return avatar.strip()
    return None


async def _load_league(
    sleeper: HomeSleeper,
    league_id: str,
) -> (
    tuple[
        SleeperLeaguePayload,
        tuple[SleeperUserPayload, ...],
        tuple[Mapping[str, Any], ...],
        tuple[SleeperRosterPayload, ...],
        SleeperStatePayload,
    ]
    | None
):
    try:
        raw_league = await sleeper.league(league_id)
        raw_user_list = await sleeper.users(league_id)
        raw_roster_list = await sleeper.rosters(league_id)
        raw_state = await sleeper.state()
        if not isinstance(raw_user_list, list) or not isinstance(raw_roster_list, list):
            return None
        raw_users = tuple(item for item in raw_user_list if isinstance(item, Mapping))
        if len(raw_users) != len(raw_user_list):
            return None
        league = SleeperLeaguePayload.model_validate(raw_league)
        users = tuple(SleeperUserPayload.model_validate(item) for item in raw_users)
        rosters = tuple(SleeperRosterPayload.model_validate(item) for item in raw_roster_list)
        state = SleeperStatePayload.model_validate(raw_state)
    except (CloudflareProviderError, ValidationError, TypeError, ValueError):
        return None
    return league, users, raw_users, rosters, state


async def _matchup(
    sleeper: HomeSleeper,
    users: Sequence[SleeperUserPayload],
    raw_users: Sequence[Mapping[str, Any]],
    rosters: Sequence[SleeperRosterPayload],
    manager_roster: SleeperRosterPayload,
    *,
    league_id: str,
    user_id: str,
    week: int,
) -> tuple[HomeSide, HomeSide | None] | None:
    manager_user = next((user for user in users if user.user_id == user_id), None)
    manager_side = HomeSide(
        name=_display_name(manager_user),
        score=None,
        image_url=avatar_image_url(_user_avatar(raw_users, user_id)),
    )
    if week == 0:
        return manager_side, None
    try:
        payload = await sleeper.matchups(league_id, week)
    except (CloudflareProviderError, TypeError, ValueError):
        return None
    sides = matchup_sides(payload)
    if sides is None:
        return None
    mine = tuple(side for side in sides if side.roster_id == manager_roster.roster_id)
    if sides and len(mine) != 1:
        return None
    if not mine:
        return manager_side, None
    manager_side = HomeSide(
        name=manager_side.name,
        score=mine[0].points,
        image_url=manager_side.image_url,
    )
    if mine[0].matchup_id is None:
        return manager_side, None
    opponents = tuple(
        side
        for side in sides
        if side.matchup_id == mine[0].matchup_id and side.roster_id != manager_roster.roster_id
    )
    if len(opponents) != 1:
        return manager_side, None
    opponent_roster = next(
        (roster for roster in rosters if roster.roster_id == opponents[0].roster_id),
        None,
    )
    if opponent_roster is None or opponent_roster.owner_id is None:
        return manager_side, HomeSide(name=None, score=opponents[0].points, image_url=None)
    opponent_user = next(
        (user for user in users if user.user_id == opponent_roster.owner_id),
        None,
    )
    return manager_side, HomeSide(
        name=_display_name(opponent_user),
        score=opponents[0].points,
        image_url=avatar_image_url(_user_avatar(raw_users, opponent_roster.owner_id)),
    )


async def _games(
    espn: HomeSchedule,
    repository: AsyncRuntimeStateRepository,
    rows: Sequence[RosterRow],
    identities: Mapping[str, HomeIdentity],
    *,
    season_year: int,
    timezone_name: str,
    now: datetime,
) -> tuple[_SectionStatus, tuple[GameRow, ...]]:
    del identities
    window = eastern_fantasy_week(now)
    teams = sorted({row.nba_team for row in rows if row.nba_team})
    collected: list[GameRow] = []
    for team in teams:
        games = await _team_games(
            espn,
            repository,
            team,
            season_year=season_year,
            timezone_name=timezone_name,
            now=now,
        )
        if games is None:
            return "unavailable", ()
        labeled = label_team_games(team, games, window)
        if labeled is None:
            return "unavailable", ()
        team_key = normalize_team(team)
        players = [row for row in rows if row.nba_team and normalize_team(row.nba_team) == team_key]
        collected.extend(
            GameRow(
                start=game.start,
                player_name=player.name,
                espn_id=player.espn_id,
                opponent=game.opponent,
                home=game.home,
            )
            for player in players
            for game in labeled
        )
    if not collected:
        return "empty", ()
    ordered = tuple(sorted(collected, key=lambda item: (item.start, item.player_name or "")))
    return "ready", ordered


async def _team_games(
    espn: HomeSchedule,
    repository: AsyncRuntimeStateRepository,
    team: str,
    *,
    season_year: int,
    timezone_name: str,
    now: datetime,
) -> tuple[ScheduledGame, ...] | None:
    local_day = manager_local_day(now, timezone_name=timezone_name)
    key = f"home-schedule:{team}:{season_year}:{local_day.isoformat()}"
    cached = decode_schedule(await read_home_cache(repository, key, now), now)
    if cached is not None:
        return cached
    try:
        result = await espn.team_schedule(team, season_year)
    except Exception:
        return None
    if result.quality.state is DataQualityState.ERROR:
        return None
    await write_home_cache(
        repository,
        key,
        [
            {
                "start": game.start_time.isoformat(),
                "status": game.status.value,
                "home": game.home_abbreviation,
                "away": game.away_abbreviation,
            }
            for game in result.records
        ],
        resource="team-schedule",
        timezone_name=timezone_name,
        now=now,
    )
    return result.records


async def _recommendation(
    repository: AsyncRuntimeStateRepository,
    league_id: str,
    week: int,
) -> tuple[_SectionStatus, str | None, str | None, str | None, datetime | None]:
    try:
        record = await repository.latest_current_recommendation(
            league_id,
            week,
            decision_type=_WEEKLY_LINEUP,
        )
    except Exception:
        return "unavailable", None, None, None, None
    if record is None:
        return "empty", None, None, None, None
    return (
        "ready",
        plan_status_from_trace(record.trace_json),
        record.title,
        record.message,
        record.deadline,
    )
