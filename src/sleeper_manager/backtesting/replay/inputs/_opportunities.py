"""Enumerate rostered opportunities from schedules and separate NBA-team observations."""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalReplayBuildInput,
    ReplayInputExclusion,
)
from sleeper_manager.backtesting.replay.roster_timeline import FantasyWeekBoundary
from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.integrations.nba.identity import PlayerMapping


@dataclass(frozen=True, slots=True)
class ExpectedInventory:
    """Known player-game keys and unresolved evidence; unknown counts are never invented."""

    teams: dict[tuple[str, str], str]
    inferred_count: int
    exclusions: tuple[ReplayInputExclusion, ...]


def expected_inventory(
    inputs: HistoricalReplayBuildInput,
    boundary: FantasyWeekBoundary,
    roster_id: int,
    schedule: Mapping[str, ScheduledGame],
    mappings: Mapping[str, PlayerMapping],
) -> ExpectedInventory:
    """Infer membership only between agreeing observations, without reading box-score rows."""
    teams: dict[tuple[str, str], str] = {}
    issues: list[ReplayInputExclusion] = []
    inferred_count = 0
    players = inputs.roster_timeline.players_overlapping(
        roster_id, boundary.utc_start, boundary.utc_end
    )
    games = tuple(
        game
        for game in schedule.values()
        if boundary.utc_start <= game.start_time < boundary.utc_end
        and game.status not in {GameStatus.POSTPONED, GameStatus.CANCELED}
    )
    for player in players:
        scope = f"week={boundary.week}:roster={roster_id}:sleeper-player={player}"
        providers = tuple(key for key, mapping in mappings.items() if mapping.sleeper_id == player)
        if len(providers) != 1:
            detail = next(
                (
                    mapping.reason
                    for mapping in inputs.player_mappings
                    if mapping.sleeper_id == player and mapping.espn_id is None
                ),
                "Expected games require one unambiguous provider identity.",
            )
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    scope,
                    detail,
                )
            )
            continue
        history: dict[datetime, set[str]] = defaultdict(set)
        for observation in inputs.team_observations:
            if observation.provider_player_id == providers[0]:
                history[observation.observed_at].add(observation.team_id)
        times = sorted(history)
        if not times:
            issues.append(_unknown_team(scope))
            continue
        for game in games:
            membership = inputs.roster_timeline.membership_intervals_at(
                roster_id, player, game.start_time
            )
            if not membership:
                continue
            if len(membership) != 1:
                issues.append(
                    ReplayInputExclusion(
                        PlanningReasonCode.MISSING_MEMBERSHIP,
                        f"{scope}:game={game.provider_id}",
                        "Fantasy roster membership is ambiguous at tipoff.",
                    )
                )
                continue
            team, inferred = _team_at(history, times, game.start_time)
            if team is None:
                issues.append(_unknown_team(scope))
                continue
            if team in (game.home_team_id, game.away_team_id):
                teams[(player, game.provider_id)] = team
                inferred_count += int(inferred)
    return ExpectedInventory(teams, inferred_count, tuple(dict.fromkeys(issues)))


def _team_at(
    history: Mapping[datetime, set[str]], times: list[datetime], at: datetime
) -> tuple[str | None, bool]:
    """Do not extrapolate past an endpoint or bridge differing/ambiguous team records."""
    index = bisect_left(times, at)
    if index < len(times) and times[index] == at:
        choices = history[at]
        return (next(iter(choices)), False) if len(choices) == 1 else (None, False)
    if index == 0 or index == len(times):
        return None, False
    before, after = history[times[index - 1]], history[times[index]]
    if len(before) == 1 and before == after:
        return next(iter(before)), True
    return None, False


def _unknown_team(scope: str) -> ReplayInputExclusion:
    """Keep trade gaps, missing endpoints and absent players out of complete coverage."""
    return ReplayInputExclusion(
        PlanningReasonCode.MISSING_NBA_TEAM_HISTORY,
        scope,
        "Historical NBA membership is missing, conflicting, or not bracketed by agreeing teams.",
    )
