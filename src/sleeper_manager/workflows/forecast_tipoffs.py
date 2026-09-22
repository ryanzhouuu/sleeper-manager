"""Remember today's relevant tipoffs in the live-state cache.

A successful slate is reused until the manager-local day ends so later wakes do
not download the player catalog. A failed lookup is left uncached. The forecast
archive is not used for this schedule.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.nba import (
    DataQualityState,
    GameStatus,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
)
from sleeper_manager.domain.scheduling import manager_local_day, manager_local_day_window
from sleeper_manager.integrations.nba.identity import (
    PlayerIdentityMapper,
    PlayerMapping,
    parse_sleeper_player_identity,
)
from sleeper_manager.persistence.base import AsyncNBADataCache, CachedNBARecord

TIPOFF_CACHE_PROVIDER = "forecast-capture"
TIPOFF_CACHE_SCHEMA_VERSION = "1"


class ForecastTipoffError(ValueError):
    """Raised when manager and opponent tipoffs cannot be resolved safely."""


class TeamScheduleSource(Protocol):
    """NBA roster and schedule reads needed to place rostered players."""

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]: ...

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]: ...


@dataclass(frozen=True, slots=True)
class MatchupSide:
    """One roster side of a Sleeper fantasy matchup."""

    roster_id: int
    matchup_id: int | None
    player_ids: tuple[str, ...]


def tipoff_cache_key(league_id: str, local_day: date) -> str:
    """Name the live-state row that holds one league's local-day tipoffs."""

    return f"forecast-tipoffs:{league_id}:{local_day.isoformat()}"


def encode_tipoffs(tipoffs: tuple[datetime, ...]) -> str:
    """Serialize aware tipoff instants in chronological order."""

    return json.dumps(
        [tipoff.astimezone(UTC).isoformat() for tipoff in sorted(tipoffs)],
        separators=(",", ":"),
    )


def decode_tipoffs(payload: str) -> tuple[datetime, ...]:
    """Decode a cached tipoff list. Raises ValueError when the payload is unusable."""

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("Forecast tipoff cache is not JSON") from error
    if not isinstance(value, list):
        raise ValueError("Forecast tipoff cache must be a list")
    tipoffs: list[datetime] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Forecast tipoff cache entry must be text")
        parsed = datetime.fromisoformat(item)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Forecast tipoff cache entry must be timezone-aware")
        tipoffs.append(parsed)
    return tuple(tipoffs)


async def tipoffs_for_capture(
    cache: AsyncNBADataCache,
    *,
    league_id: str,
    now: datetime,
    timezone_name: str,
    in_season: bool,
    resolve: Callable[[], Awaitable[tuple[datetime, ...]]],
) -> tuple[datetime, ...]:
    """Return cached tipoffs, or resolve and store them through the local day.

    Outside the season this does not read or write the cache. A resolve failure
    propagates and leaves the previous entry unchanged.
    """

    if not in_season:
        return ()
    local_day = manager_local_day(now, timezone_name=timezone_name)
    key = tipoff_cache_key(league_id, local_day)
    cached = await cache.get(key, now=now)
    if cached is not None and cached.quality is DataQualityState.FRESH:
        try:
            return decode_tipoffs(cached.payload_json)
        except ValueError:
            pass
    tipoffs = await resolve()
    window = manager_local_day_window(now, timezone_name=timezone_name)
    await cache.put(
        CachedNBARecord(
            cache_key=key,
            provider=TIPOFF_CACHE_PROVIDER,
            resource="relevant-tipoffs",
            schema_version=TIPOFF_CACHE_SCHEMA_VERSION,
            payload_json=encode_tipoffs(tipoffs),
            retrieved_at=now,
            source_updated_at=None,
            expires_at=window.ends_at,
            quality=DataQualityState.FRESH,
        )
    )
    return tipoffs


def parse_matchup_sides(payload: object) -> tuple[MatchupSide, ...]:
    """Read roster, matchup, and player fields from a Sleeper matchup list."""

    if not isinstance(payload, list):
        raise ForecastTipoffError("Sleeper matchups must be a list")
    sides: list[MatchupSide] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise ForecastTipoffError(f"Sleeper matchup {index} must be an object")
        sides.append(_matchup_side(item, index))
    return tuple(sides)


def opponent_roster_id(sides: Sequence[MatchupSide], manager_roster_id: int) -> int | None:
    """Return the current opponent roster, or None when the manager has a bye."""

    mine = tuple(side for side in sides if side.roster_id == manager_roster_id)
    if len(mine) != 1:
        raise ForecastTipoffError("Manager matchup side is missing")
    matchup_id = mine[0].matchup_id
    if matchup_id is None:
        return None
    opponents = tuple(
        side.roster_id
        for side in sides
        if side.matchup_id == matchup_id and side.roster_id != manager_roster_id
    )
    if len(opponents) != 1:
        raise ForecastTipoffError("Forecast matchup opponent is ambiguous")
    return opponents[0]


def capture_roster_player_ids(profile: LeagueProfile, matchups: object) -> tuple[str, ...]:
    """Combine the manager roster with the current opponent roster."""

    opponent_id = opponent_roster_id(parse_matchup_sides(matchups), profile.manager_roster_id)
    player_ids = list(_roster_player_ids(profile, profile.manager_roster_id))
    if opponent_id is not None:
        player_ids.extend(_roster_player_ids(profile, opponent_id))
    return tuple(dict.fromkeys(player_ids))


def relevant_tipoff_times(
    games: Iterable[ScheduledGame],
    team_ids: Iterable[str],
) -> tuple[datetime, ...]:
    """Return scheduled start times for the supplied NBA teams."""

    teams = set(team_ids)
    return tuple(
        sorted(
            {
                game.start_time
                for game in games
                if game.status is GameStatus.SCHEDULED
                and (game.home_team_id in teams or game.away_team_id in teams)
            }
        )
    )


async def resolve_relevant_tipoffs(
    *,
    player_ids: tuple[str, ...],
    catalog: Mapping[str, Mapping[str, Any]],
    nba: TeamScheduleSource,
    season: str,
    mapping_overrides: Mapping[str, str],
) -> tuple[datetime, ...]:
    """Map rostered players to scheduled NBA games. Raises ForecastTipoffError on gaps."""

    if any(player_id not in catalog for player_id in player_ids):
        raise ForecastTipoffError("Sleeper catalog is missing rostered players")
    try:
        identities = tuple(
            parse_sleeper_player_identity(player_id, catalog[player_id]) for player_id in player_ids
        )
        season_year = int(season)
    except (ValueError, TypeError) as error:
        raise ForecastTipoffError("Forecast roster identity is incomplete") from error
    abbreviations = sorted({identity.team for identity in identities if identity.team})
    provider_players: list[ProviderPlayer] = []
    for abbreviation in abbreviations:
        roster = await nba.team_roster(abbreviation)
        _require_fresh(roster)
        provider_players.extend(roster.records)
    mapped = PlayerIdentityMapper().resolve(
        identities,
        provider_players,
        overrides=mapping_overrides,
    )
    players_by_provider = {player.provider_id: player for player in provider_players}
    team_ids = _provider_team_ids(mapped.mappings, players_by_provider)
    games: list[ScheduledGame] = []
    for team_id in team_ids:
        schedule = await nba.team_schedule(team_id, season_year)
        _require_fresh(schedule)
        games.extend(schedule.records)
    return relevant_tipoff_times(games, team_ids)


def _provider_team_ids(
    mappings: Sequence[PlayerMapping],
    players_by_provider: Mapping[str, ProviderPlayer],
) -> list[str]:
    """Collect NBA team ids for players that resolved to a provider roster."""

    found: set[str] = set()
    for mapping in mappings:
        if mapping.espn_id is None:
            continue
        player = players_by_provider.get(mapping.espn_id)
        if player is None or not player.team_id:
            continue
        found.add(player.team_id)
    return sorted(found)


def _require_fresh(result: ProviderResult[Any]) -> None:
    """Reject an errored provider read before it can be cached as an empty slate."""

    if result.quality.state is DataQualityState.ERROR:
        raise ForecastTipoffError(f"NBA resource {result.quality.resource} is unavailable")


def _matchup_side(item: Mapping[str, Any], index: int) -> MatchupSide:
    """Validate one matchup object."""

    roster_id = item.get("roster_id")
    matchup_id = item.get("matchup_id")
    players = item.get("players")
    if isinstance(roster_id, bool) or not isinstance(roster_id, int):
        raise ForecastTipoffError(f"Sleeper matchup {index} roster is invalid")
    if matchup_id is not None and (isinstance(matchup_id, bool) or not isinstance(matchup_id, int)):
        raise ForecastTipoffError(f"Sleeper matchup {index} id is invalid")
    if not isinstance(players, list) or not all(
        isinstance(player, str) and player for player in players
    ):
        raise ForecastTipoffError(f"Sleeper matchup {index} players are invalid")
    return MatchupSide(roster_id, matchup_id, tuple(players))


def _roster_player_ids(profile: LeagueProfile, roster_id: int) -> tuple[str, ...]:
    """Return the one profile roster selected by Sleeper."""

    matches = tuple(
        roster.player_ids for roster in profile.rosters if roster.roster_id == roster_id
    )
    if len(matches) != 1:
        raise ForecastTipoffError("Forecast roster is missing")
    return matches[0]


__all__ = (
    "ForecastTipoffError",
    "MatchupSide",
    "TeamScheduleSource",
    "capture_roster_player_ids",
    "decode_tipoffs",
    "encode_tipoffs",
    "opponent_roster_id",
    "parse_matchup_sides",
    "relevant_tipoff_times",
    "resolve_relevant_tipoffs",
    "tipoff_cache_key",
    "tipoffs_for_capture",
)
