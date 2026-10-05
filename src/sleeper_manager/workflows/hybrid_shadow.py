"""Paired candidate/control evidence without changing production provider selection."""

from dataclasses import dataclass

from sleeper_manager.backtesting.artifacts import canonical_json, sha256_text
from sleeper_manager.backtesting.replay.hybrid_projection_surface import HybridSurfaceBuild
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.projection_surface import (
    validate_historical_projection_surface,
)
from sleeper_manager.backtesting.replay.projection_surface_models import (
    HistoricalProjectionSurface,
    ProjectionSurfaceEntry,
)
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_model import HybridProjectionResult


@dataclass(frozen=True, slots=True)
class HybridShadowPair:
    roster_id: int
    control: ProjectionSurfaceEntry
    candidate: HybridProjectionResult
    expected_score_delta: float | None


@dataclass(frozen=True, slots=True)
class HybridShadowComparison:
    model_version: str
    candidate_fingerprints: tuple[str, ...]
    control_fingerprints: tuple[str, ...]
    pairs: tuple[HybridShadowPair, ...]
    schema_version: str = "opening-week-hybrid-shadow-v1"

    @property
    def fingerprint(self) -> str:
        return sha256_text(canonical_json(self))


def compare_hybrid_shadow(
    team_weeks: tuple[HistoricalTeamWeekInput, ...],
    candidate: HybridSurfaceBuild,
    controls: tuple[HistoricalProjectionSurface, ...],
) -> HybridShadowComparison:
    """Require matching cutoffs and scoring before comparing the same opportunities."""
    if (
        not team_weeks
        or len(candidate.surfaces) != len(team_weeks)
        or len(controls) != len(team_weeks)
    ):
        raise HybridProjectionError("shadow_surface_count_mismatch")
    models = {surface.projection_config_version for surface in candidate.surfaces}
    if len(models) != 1:
        raise HybridProjectionError("shadow_candidate_model_mismatch")
    attempts = {
        (row.cutoff, row.target.sleeper_player_id, row.target.game_id): row
        for row in candidate.attempts
    }
    pairs: list[HybridShadowPair] = []
    for week, surface, control in zip(team_weeks, candidate.surfaces, controls, strict=True):
        validate_historical_projection_surface(surface, team_week=week)
        validate_historical_projection_surface(control, team_week=week)
        if (
            surface.scoring_policy_version != control.scoring_policy_version
            or surface.planning_lead_time_seconds != control.planning_lead_time_seconds
        ):
            raise HybridProjectionError("shadow_scoring_or_cutoff_mismatch")
        control_by_key = {entry.key: entry for entry in control.entries}
        for entry in surface.entries:
            attempt = attempts.get(entry.key)
            if (
                attempt is None
                or attempt.projection != entry.projection
                or attempt.failure != entry.failure_reason
            ):
                raise HybridProjectionError("shadow_attempt_evidence_mismatch")
            baseline = control_by_key[entry.key]
            delta = (
                entry.projection.distribution.expected_value
                - baseline.projection.distribution.expected_value
                if entry.projection is not None and baseline.projection is not None
                else None
            )
            pairs.append(HybridShadowPair(week.roster_id, baseline, attempt, delta))
    return HybridShadowComparison(
        next(iter(models)),
        tuple(surface.fingerprint for surface in candidate.surfaces),
        tuple(surface.fingerprint for surface in controls),
        tuple(pairs),
    )
