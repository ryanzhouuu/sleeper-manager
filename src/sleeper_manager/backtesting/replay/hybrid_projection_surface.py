"""Generate complete candidate sidecars using the existing replay cutoff schedule."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sleeper_manager.backtesting.artifacts import canonical_json, sha256_text
from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalTeamWeekInput,
    SourceFingerprint,
)
from sleeper_manager.backtesting.replay.projection_surface import (
    full_advisor_planning_cutoffs,
    team_week_fingerprint,
    validate_historical_projection_surface,
)
from sleeper_manager.backtesting.replay.projection_surface_models import (
    HistoricalProjectionSurface,
    ProjectionSurfaceEntry,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_model import HybridProjectionResult
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget


@dataclass(frozen=True, slots=True)
class HybridSurfaceBuild:
    surfaces: tuple[HistoricalProjectionSurface, ...]
    attempts: tuple[HybridProjectionResult, ...]


def build_hybrid_projection_surfaces(
    team_weeks: tuple[HistoricalTeamWeekInput, ...],
    provider: HybridProjectionProvider,
    *,
    scoring_policy: ScoringPolicy,
    planning_lead_time: timedelta = timedelta(minutes=10),
    availability: Mapping[tuple[datetime, str, str], GameAvailability] | None = None,
) -> HybridSurfaceBuild:
    """Batch shared targets across rosters; preserve every failed projection attempt."""
    if not team_weeks or len({week.roster_id for week in team_weeks}) != len(team_weeks):
        raise HybridProjectionError("missing_or_duplicate_team_week")
    if len({(week.league_id, week.season, week.week) for week in team_weeks}) != 1:
        raise HybridProjectionError("team_week_scope_mismatch")
    cutoffs = {
        week.roster_id: full_advisor_planning_cutoffs(week, planning_lead_time)
        for week in team_weeks
    }
    games = {week.roster_id: {game.game_id: game for game in week.games} for week in team_weeks}
    entries: dict[int, list[ProjectionSurfaceEntry]] = {week.roster_id: [] for week in team_weeks}
    attempts: list[HybridProjectionResult] = []
    for cutoff in sorted({cutoff for values in cutoffs.values() for cutoff in values}):
        targets: dict[tuple[str, str], LiveProjectionTarget] = {}
        owners: dict[int, list[tuple[str, str]]] = {}
        for week in team_weeks:
            if cutoff not in cutoffs[week.roster_id]:
                continue
            owners[week.roster_id] = []
            for row in week.player_games:
                game = games[week.roster_id][row.game_id]
                if game.start_time <= cutoff:
                    continue
                target = LiveProjectionTarget(
                    row.sleeper_id, row.game_id, game.start_time, row.provider_player_id or None
                )
                key = row.sleeper_id, row.game_id
                if key in targets and targets[key] != target:
                    raise HybridProjectionError("conflicting_shared_target")
                targets[key] = target
                owners[week.roster_id].append(key)
        results = provider.project_batch(
            tuple(targets[key] for key in sorted(targets)),
            scoring_policy=scoring_policy,
            decision_time=cutoff,
            availability={
                (player, game): report
                for (at, player, game), report in (availability or {}).items()
                if at == cutoff
            },
        )
        attempts.extend(results)
        by_key = {
            (result.target.sleeper_player_id, result.target.game_id): result for result in results
        }
        for roster, keys in owners.items():
            for player, game_id in keys:
                result = by_key[player, game_id]
                entries[roster].append(
                    ProjectionSurfaceEntry(
                        cutoff,
                        player,
                        game_id,
                        projection=result.projection,
                        failure_reason=result.failure,
                        failure_detail=result.failure,
                    )
                )
    evidence_hash = sha256_text(canonical_json(tuple(attempts)))
    surfaces = tuple(
        HistoricalProjectionSurface(
            team_week_manifest_id=week.manifest_id,
            league_id=week.league_id,
            season=week.season,
            week=week.week,
            roster_id=week.roster_id,
            team_week_fingerprint=team_week_fingerprint(week),
            projection_config_version=provider.model_version,
            scoring_policy_version=scoring_policy.version,
            source_fingerprints=(
                SourceFingerprint("hybrid-cutoff-evidence", evidence_hash, "hybrid-evidence-v1"),
            ),
            entries=tuple(entries[week.roster_id]),
            planning_lead_time_seconds=planning_lead_time.total_seconds(),
        )
        for week in team_weeks
    )
    for week, surface in zip(team_weeks, surfaces, strict=True):
        validate_historical_projection_surface(
            surface,
            team_week=week,
            projection_config_version=provider.model_version,
            planning_lead_time=planning_lead_time,
        )
    return HybridSurfaceBuild(surfaces, tuple(attempts))
