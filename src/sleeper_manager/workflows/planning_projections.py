"""Build player-game projection targets independently of live network collection."""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_availability import constrain_live_availability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    LiveProjectionProvider,
    LiveProjectionResult,
    ResolvedPlayerIdentity,
    ScheduleResourceResult,
)


@runtime_checkable
class LiveBatchProjectionProvider(Protocol):
    """Batch snapshots in target order while the implementation retains full diagnostics."""

    async def project_live_batch(
        self,
        targets: tuple[LiveProjectionTarget, ...],
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
        availability: Mapping[tuple[str, str], GameAvailability],
    ) -> tuple[ProjectionSnapshot | None, ...]: ...


def live_projection_targets(
    *,
    profile: LeagueProfile,
    identities: tuple[ResolvedPlayerIdentity, ...],
    schedule_results: list[ScheduleResourceResult],
    week_window: FantasyWeekWindow,
    decision_time: datetime,
) -> tuple[LiveProjectionTarget, ...]:
    """Enumerate distinct roster player-games; model-specific started-game handling stays intact."""
    games_by_team: dict[str, dict[str, datetime]] = defaultdict(dict)
    for result in schedule_results:
        if result.quality.state is DataQualityState.ERROR:
            continue
        for game in result.games:
            if not week_window.contains(game.start_time):
                continue
            for team_id in (game.home_team_id, game.away_team_id):
                games_by_team[team_id][game.provider_id] = game.start_time

    targets: list[LiveProjectionTarget] = []
    seen: set[tuple[str, str]] = set()
    for identity in identities:
        if identity.provider_player_id is None or identity.provider_team_id is None:
            continue
        for game_id, game_start in sorted(games_by_team.get(identity.provider_team_id, {}).items()):
            key = identity.sleeper_player_id, game_id
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                LiveProjectionTarget(
                    sleeper_player_id=identity.sleeper_player_id,
                    game_id=game_id,
                    game_start=game_start,
                    provider_player_id=identity.provider_player_id,
                )
            )
    return tuple(targets)


async def project_live_targets(
    targets: tuple[LiveProjectionTarget, ...],
    *,
    projection_provider: LiveProjectionProvider | LiveBatchProjectionProvider,
    scoring_policy: ScoringPolicy,
    decision_time: datetime,
    availability: Mapping[tuple[str, str], GameAvailability],
    max_availability_age: timedelta,
) -> tuple[LiveProjectionResult, ...]:
    """Use one async batch when supported; enforce availability on either provider boundary."""
    if isinstance(projection_provider, LiveBatchProjectionProvider):
        snapshots = await projection_provider.project_live_batch(
            targets,
            scoring_policy=scoring_policy,
            decision_time=decision_time,
            availability=availability,
        )
    else:
        snapshots = tuple(
            projection_provider.project(
                target,
                scoring_policy=scoring_policy,
                decision_time=decision_time,
            )
            for target in targets
        )
    if len(snapshots) != len(targets):
        raise ValueError("Projection batch does not match targets")
    projections = []
    for target, snapshot in zip(targets, snapshots, strict=True):
        if snapshot is not None and (snapshot.player_id, snapshot.game_id) != (
            target.sleeper_player_id,
            target.game_id,
        ):
            raise ValueError("Projection batch returned a different player/game")
        snapshot = constrain_live_availability(
            snapshot,
            target,
            availability.get((target.sleeper_player_id, target.game_id)),
            cutoff=decision_time,
            scoring_policy=scoring_policy,
            max_age=max_availability_age,
        )
        projections.append(
            LiveProjectionResult(
                target.sleeper_player_id,
                target.game_id,
                snapshot,
                None if snapshot is not None else PlanningReasonCode.MISSING_PROJECTION,
            )
        )
    return tuple(projections)
