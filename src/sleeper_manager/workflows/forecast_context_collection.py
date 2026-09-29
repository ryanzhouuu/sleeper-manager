"""Read roster and schedule evidence at the moment a forecast slot is persisted.

The day tipoff cache can still decide whether a slot is due. This module records
what that slot could see, including an explicit gap when a companion read fails.
Later replay must use the stored snapshot rather than the live roster.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sleeper_manager.domain.forecast_context import (
    ForecastCaptureContext,
    ForecastContextEligibility,
    ForecastContextGame,
    ForecastContextGap,
    ForecastContextGapCode,
    ForecastContextRoster,
)
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.nba import DataQualityState, ProviderPlayer, ScheduledGame
from sleeper_manager.domain.scheduling import eastern_fantasy_week
from sleeper_manager.integrations.nba.identity import (
    PlayerIdentityMapper,
    parse_sleeper_player_identity,
)
from sleeper_manager.workflows.forecast_tipoffs import (
    ForecastTipoffError,
    TeamScheduleSource,
    opponent_roster_id,
    parse_matchup_sides,
)


class ForecastContextSleeper(Protocol):
    """Matchup and catalog reads needed to describe one capture slot."""

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]: ...

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ForecastContextObservation:
    """Companion evidence collected before a receipt persistence time is known."""

    league_id: str
    season: str
    week: int
    matchup_id: int | None
    bye: bool
    manager: ForecastContextRoster | None
    opponent: ForecastContextRoster | None
    eligibility: tuple[ForecastContextEligibility, ...]
    games: tuple[ForecastContextGame, ...]
    gaps: tuple[ForecastContextGap, ...]


def bind_forecast_context(
    observation: ForecastContextObservation,
    *,
    receipt_id: str,
    persisted_at: datetime,
) -> ForecastCaptureContext:
    """Attach one observation to the receipt that will store it."""

    return ForecastCaptureContext(
        receipt_id=receipt_id,
        league_id=observation.league_id,
        season=observation.season,
        week=observation.week,
        matchup_id=observation.matchup_id,
        bye=observation.bye,
        manager=observation.manager,
        opponent=observation.opponent,
        eligibility=observation.eligibility,
        games=observation.games,
        gaps=observation.gaps,
        persisted_at=persisted_at,
    )


def forecast_context_gap_observation(
    profile: LeagueProfile,
    *,
    detail: str,
) -> ForecastContextObservation:
    """Explain a companion failure without dropping the forecast receipt."""

    return ForecastContextObservation(
        league_id=profile.league_id,
        season=profile.season,
        week=profile.fantasy_week.week,
        matchup_id=None,
        bye=False,
        manager=None,
        opponent=None,
        eligibility=(),
        games=(),
        gaps=(
            ForecastContextGap(ForecastContextGapCode.MATCHUP, profile.league_id, detail),
            ForecastContextGap(ForecastContextGapCode.ROSTER, "manager", detail),
        ),
    )


async def observe_forecast_context(
    sleeper: ForecastContextSleeper,
    nba: TeamScheduleSource,
    *,
    profile: LeagueProfile,
    mapping_overrides: Mapping[str, str],
    read_at: datetime,
) -> ForecastContextObservation:
    """Read companion sources, converting a failed read into an explicit gap."""

    matchups: object | None
    catalog: Mapping[str, Mapping[str, Any]] | None
    try:
        matchups = await sleeper.matchups(profile.league_id, profile.fantasy_week.week)
        matchup_error = None
    except Exception as error:
        matchups = None
        matchup_error = type(error).__name__
    try:
        catalog = await sleeper.players(active=True)
        catalog_error = None
    except Exception as error:
        catalog = None
        catalog_error = type(error).__name__
    return await collect_forecast_context(
        nba,
        profile=profile,
        matchups=matchups,
        catalog=catalog,
        mapping_overrides=mapping_overrides,
        read_at=read_at,
        matchup_error=matchup_error,
        catalog_error=catalog_error,
    )


async def collect_forecast_context(
    nba: TeamScheduleSource,
    *,
    profile: LeagueProfile,
    matchups: object | None,
    catalog: Mapping[str, Mapping[str, Any]] | None,
    mapping_overrides: Mapping[str, str],
    read_at: datetime,
    matchup_error: str | None = None,
    catalog_error: str | None = None,
) -> ForecastContextObservation:
    """Build one slot's roster, eligibility, and current-week game snapshot.

    Games are those whose start falls in the Eastern fantasy week containing
    ``read_at``. A bye stores no opponent. Missing companion sources become gaps.
    """

    gaps: list[ForecastContextGap] = []
    matchup_id, bye, opponent_id, matchup_gaps = _matchup(profile, matchups, matchup_error)
    gaps.extend(matchup_gaps)
    manager = _roster(profile, profile.manager_roster_id, read_at)
    if manager is None:
        gaps.append(
            ForecastContextGap(
                ForecastContextGapCode.ROSTER,
                "manager",
                "manager roster is missing",
            )
        )
    opponent = None
    if not bye and opponent_id is not None:
        opponent = _roster(profile, opponent_id, read_at)
        if opponent is None:
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.ROSTER,
                    str(opponent_id),
                    "opponent roster is missing",
                )
            )
    player_ids = _player_ids(manager, opponent)
    eligibility, eligibility_gaps = _eligibility(
        player_ids,
        catalog,
        read_at,
        catalog_error,
    )
    gaps.extend(eligibility_gaps)
    games, game_gaps = await _games(
        nba,
        profile=profile,
        player_ids=player_ids,
        catalog=None if catalog_error else catalog,
        mapping_overrides=mapping_overrides,
        read_at=read_at,
    )
    gaps.extend(game_gaps)
    if catalog_error and player_ids:
        gaps.append(
            ForecastContextGap(
                ForecastContextGapCode.IDENTITY,
                "catalog",
                catalog_error,
            )
        )
    return ForecastContextObservation(
        league_id=profile.league_id,
        season=profile.season,
        week=profile.fantasy_week.week,
        matchup_id=matchup_id,
        bye=bye,
        manager=manager,
        opponent=opponent,
        eligibility=eligibility,
        games=games,
        gaps=tuple(gaps),
    )


def _matchup(
    profile: LeagueProfile,
    matchups: object | None,
    matchup_error: str | None,
) -> tuple[int | None, bool, int | None, tuple[ForecastContextGap, ...]]:
    """Return matchup id, bye, and opponent roster id, or one matchup gap."""

    if matchup_error is not None or matchups is None:
        return (
            None,
            False,
            None,
            (
                ForecastContextGap(
                    ForecastContextGapCode.MATCHUP,
                    profile.league_id,
                    matchup_error or "matchup read failed",
                ),
            ),
        )
    try:
        sides = parse_matchup_sides(matchups)
        opponent_id = opponent_roster_id(sides, profile.manager_roster_id)
        mine = next(side for side in sides if side.roster_id == profile.manager_roster_id)
    except ForecastTipoffError as error:
        return (
            None,
            False,
            None,
            (
                ForecastContextGap(
                    ForecastContextGapCode.MATCHUP,
                    profile.league_id,
                    str(error),
                ),
            ),
        )
    bye = mine.matchup_id is None
    return mine.matchup_id, bye, None if bye else opponent_id, ()


def _roster(
    profile: LeagueProfile,
    roster_id: int,
    read_at: datetime,
) -> ForecastContextRoster | None:
    """Copy one profile roster, preserving starter and reserve order."""

    matches = tuple(roster for roster in profile.rosters if roster.roster_id == roster_id)
    if len(matches) != 1:
        return None
    roster = matches[0]
    return ForecastContextRoster(
        roster_id=roster.roster_id,
        player_ids=roster.player_ids,
        starter_ids=roster.starter_ids,
        reserve_ids=roster.reserve_ids,
        read_at=read_at,
    )


def _player_ids(
    manager: ForecastContextRoster | None,
    opponent: ForecastContextRoster | None,
) -> tuple[str, ...]:
    """List rostered players once, manager first."""

    ordered: list[str] = []
    for roster in (manager, opponent):
        if roster is None:
            continue
        for player_id in roster.player_ids:
            if player_id not in ordered:
                ordered.append(player_id)
    return tuple(ordered)


def _eligibility(
    player_ids: tuple[str, ...],
    catalog: Mapping[str, Mapping[str, Any]] | None,
    read_at: datetime,
    catalog_error: str | None,
) -> tuple[tuple[ForecastContextEligibility, ...], tuple[ForecastContextGap, ...]]:
    """Read fantasy positions for rostered players, or record one catalog gap."""

    if catalog_error is not None or catalog is None:
        if not player_ids:
            return (), ()
        return (
            (),
            (
                ForecastContextGap(
                    ForecastContextGapCode.ELIGIBILITY,
                    "catalog",
                    catalog_error or "catalog read failed",
                ),
            ),
        )
    items: list[ForecastContextEligibility] = []
    gaps: list[ForecastContextGap] = []
    for player_id in player_ids:
        payload = catalog.get(player_id)
        if not isinstance(payload, Mapping):
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.ELIGIBILITY,
                    player_id,
                    "catalog is missing the player",
                )
            )
            continue
        positions = _positions(payload.get("fantasy_positions", ()))
        if positions is None:
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.ELIGIBILITY,
                    player_id,
                    "fantasy positions are invalid",
                )
            )
            continue
        items.append(ForecastContextEligibility(player_id, positions, read_at))
    return tuple(items), tuple(gaps)


def _positions(raw: object) -> tuple[str, ...] | None:
    """Strip catalog positions, dropping blanks and repeats."""

    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        return None
    if not all(isinstance(item, str) for item in raw):
        return None
    positions: list[str] = []
    for item in raw:
        cleaned = item.strip()
        if not cleaned or cleaned in positions:
            continue
        positions.append(cleaned)
    return tuple(positions)


async def _games(
    nba: TeamScheduleSource,
    *,
    profile: LeagueProfile,
    player_ids: tuple[str, ...],
    catalog: Mapping[str, Mapping[str, Any]] | None,
    mapping_overrides: Mapping[str, str],
    read_at: datetime,
) -> tuple[tuple[ForecastContextGame, ...], tuple[ForecastContextGap, ...]]:
    """Keep this fantasy week's games for rostered players' resolved teams."""

    if not player_ids:
        return (), ()
    if catalog is None:
        return (), ()
    identities = []
    gaps: list[ForecastContextGap] = []
    for player_id in player_ids:
        payload = catalog.get(player_id)
        if not isinstance(payload, Mapping):
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.IDENTITY,
                    player_id,
                    "catalog is missing the player",
                )
            )
            continue
        try:
            identities.append(parse_sleeper_player_identity(player_id, payload))
        except ValueError:
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.IDENTITY,
                    player_id,
                    "player identity is incomplete",
                )
            )
    try:
        season_year = int(profile.season)
    except ValueError:
        gaps.append(
            ForecastContextGap(
                ForecastContextGapCode.SCHEDULE,
                profile.season,
                "season is invalid",
            )
        )
        return (), tuple(gaps)
    abbreviations = sorted({identity.team for identity in identities if identity.team})
    provider_players: list[ProviderPlayer] = []
    failed_teams: set[str] = set()
    for abbreviation in abbreviations:
        try:
            roster = await nba.team_roster(abbreviation)
        except Exception:
            failed_teams.add(abbreviation)
            continue
        if roster.quality.state is DataQualityState.ERROR:
            failed_teams.add(abbreviation)
            continue
        provider_players.extend(roster.records)
    mapped = PlayerIdentityMapper().resolve(
        identities,
        provider_players,
        overrides=mapping_overrides,
    )
    players_by_provider = {player.provider_id: player for player in provider_players}
    identity_by_sleeper = {identity.sleeper_id: identity for identity in identities}
    team_ids: set[str] = set()
    for mapping in mapped.mappings:
        if mapping.espn_id is None:
            identity = identity_by_sleeper[mapping.sleeper_id]
            detail = "NBA roster read failed" if identity.team in failed_teams else mapping.reason
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.IDENTITY,
                    mapping.sleeper_id,
                    detail,
                )
            )
            continue
        player = players_by_provider.get(mapping.espn_id)
        if player is None or not player.team_id:
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.IDENTITY,
                    mapping.sleeper_id,
                    "mapped player has no NBA team",
                )
            )
            continue
        team_ids.add(player.team_id)
    window = eastern_fantasy_week(read_at)
    found: dict[str, ForecastContextGame] = {}
    for team_id in sorted(team_ids):
        try:
            schedule = await nba.team_schedule(team_id, season_year)
        except Exception:
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.SCHEDULE,
                    team_id,
                    "schedule read failed",
                )
            )
            continue
        if schedule.quality.state is DataQualityState.ERROR:
            gaps.append(
                ForecastContextGap(
                    ForecastContextGapCode.SCHEDULE,
                    team_id,
                    "schedule read failed",
                )
            )
            continue
        for game in schedule.records:
            if not window.contains(game.start_time):
                continue
            if game.home_team_id not in team_ids and game.away_team_id not in team_ids:
                continue
            conflict = _remember_game(found, game, read_at)
            if conflict is not None:
                gaps.append(conflict)
    return tuple(found.values()), tuple(gaps)


def _remember_game(
    found: dict[str, ForecastContextGame],
    game: ScheduledGame,
    read_at: datetime,
) -> ForecastContextGap | None:
    """Keep one game id, or replace a conflicting copy with a schedule gap."""

    candidate = ForecastContextGame(
        game_id=game.provider_id,
        home_team_id=game.home_team_id,
        away_team_id=game.away_team_id,
        start_time=game.start_time,
        status=game.status,
        read_at=read_at,
    )
    previous = found.get(game.provider_id)
    if previous is None:
        found[game.provider_id] = candidate
        return None
    if previous != candidate:
        del found[game.provider_id]
        return ForecastContextGap(
            ForecastContextGapCode.SCHEDULE,
            game.provider_id,
            "schedule copies disagree",
        )
    return None


__all__ = (
    "ForecastContextObservation",
    "ForecastContextSleeper",
    "bind_forecast_context",
    "collect_forecast_context",
    "forecast_context_gap_observation",
    "observe_forecast_context",
)
