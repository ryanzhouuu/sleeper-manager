"""Build player-game projection targets independently of live network collection."""

from collections import defaultdict
from datetime import datetime

from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    LiveProjectionProvider,
    LiveProjectionResult,
    ResolvedPlayerIdentity,
    ScheduleResourceResult,
)


def _project_targets(
    *,
    profile: LeagueProfile,
    identities: tuple[ResolvedPlayerIdentity, ...],
    schedule_results: list[ScheduleResourceResult],
    projection_provider: LiveProjectionProvider,
    week_window: FantasyWeekWindow,
    decision_time: datetime,
) -> list[LiveProjectionResult]:
    """Project each distinct in-week target once, preserving unavailable snapshots."""
    games_by_team: dict[str, dict[str, datetime]] = defaultdict(dict)
    for result in schedule_results:
        if result.quality.state is DataQualityState.ERROR:
            continue
        for game in result.games:
            if not week_window.contains(game.start_time):
                continue
            for team_id in (game.home_team_id, game.away_team_id):
                games_by_team[team_id][game.provider_id] = game.start_time

    projections: list[LiveProjectionResult] = []
    seen: set[tuple[str, str]] = set()
    for identity in identities:
        if identity.provider_player_id is None or identity.provider_team_id is None:
            continue
        for game_id, game_start in sorted(games_by_team.get(identity.provider_team_id, {}).items()):
            key = identity.sleeper_player_id, game_id
            if key in seen:
                continue
            seen.add(key)
            snapshot = projection_provider.project(
                LiveProjectionTarget(
                    sleeper_player_id=identity.sleeper_player_id,
                    game_id=game_id,
                    game_start=game_start,
                    provider_player_id=identity.provider_player_id,
                ),
                scoring_policy=profile.scoring,
                decision_time=decision_time,
            )
            projections.append(
                LiveProjectionResult(
                    sleeper_player_id=identity.sleeper_player_id,
                    game_id=game_id,
                    snapshot=snapshot,
                    missing_reason=(
                        None if snapshot is not None else PlanningReasonCode.MISSING_PROJECTION
                    ),
                )
            )
    return projections
